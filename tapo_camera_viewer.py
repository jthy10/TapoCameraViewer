#!/usr/bin/env python3
"""tapo-camera-viewer - local, offline viewer + controller for TP-Link Tapo cameras.

Finds Tapo cameras on whatever LAN this machine is on, pulls their RTSP feed
through a bundled ffmpeg, and serves a web page (http://127.0.0.1:8765) with
live video and camera controls. Nothing here needs internet access.
"""
import argparse
import base64
import collections
import datetime
import hashlib
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import imageio_ffmpeg
import psutil

try:
    from pytapo import Tapo
except Exception:  # advanced controls just become unavailable
    Tapo = None

# when frozen by PyInstaller, bundled files (index.html) are unpacked to sys._MEIPASS
APP_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.expanduser("~/.config/tapo-camera-viewer")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
RECORD_DIR = os.path.expanduser("~/Videos/tapo-camera-viewer")
HOST, PORT = "127.0.0.1", 8765
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
RTSP_PORT, ONVIF_PORT = 554, 2020

LOCK = threading.RLock()
DEBUG = False


def debug(*a):
    if DEBUG:
        print(time.strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- config

SERVICE = "tapo-camera-viewer"
SECRET_KEYS = ("password", "cloud_password")


def _keychain():
    """The OS keychain via `keyring`, or None if this machine doesn't have a usable one."""
    try:
        import keyring
        from keyring.backends import fail
        kr = keyring.get_keyring()
        if isinstance(kr, fail.Keyring) or "null" in type(kr).__module__:
            return None
        kr.get_password(SERVICE, "password")  # make sure it actually answers
        return kr
    except Exception:
        return None


KEYCHAIN = _keychain()
_stored = {}  # what the keychain currently holds, so we only write when something changed


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    cfg.setdefault("username", "")
    cfg.setdefault("manual_ips", [])
    cfg.setdefault("known", {})  # ip -> {"name": .., "model": ..}
    for k in SECRET_KEYS:
        in_file = cfg.get(k) or ""
        if KEYCHAIN:
            _stored[k] = KEYCHAIN.get_password(SERVICE, k) or ""
            cfg[k] = in_file or _stored[k]  # a password left in the file gets moved over on save
        else:
            cfg[k] = in_file
    return cfg


def save_config():
    data = dict(CONFIG)
    if KEYCHAIN:
        for k in SECRET_KEYS:
            data.pop(k)
            if CONFIG[k] != _stored.get(k, ""):
                if CONFIG[k]:
                    KEYCHAIN.set_password(SERVICE, k, CONFIG[k])
                else:
                    try:
                        KEYCHAIN.delete_password(SERVICE, k)
                    except Exception:
                        pass
                _stored[k] = CONFIG[k]
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_PATH)


CONFIG = load_config()
if KEYCHAIN:
    try:
        with open(CONFIG_PATH) as f:
            if any(k in json.load(f) for k in SECRET_KEYS):
                save_config()  # migrate plain-text passwords out of the file
    except Exception:
        pass


def have_creds():
    return bool(CONFIG["username"] and CONFIG["password"])


# ---------------------------------------------------------------- network discovery

def local_networks():
    """IPv4 networks this machine is attached to (excluding loopback/docker)."""
    nets = []
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True).stdout
    except Exception:
        out = ""  # no iproute2 (macOS/Windows) - handled below
    for line in out.splitlines():
        m = re.search(r"^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+/\d+)", line)
        if not m:
            continue
        ifname, cidr = m.groups()
        if ifname == "lo" or ifname.startswith(("docker", "br-", "veth", "virbr")):
            continue
        iface = ipaddress.ip_interface(cidr)
        net = iface.network
        if net.prefixlen < 24:  # don't sweep a /16; scan the /24 we live in
            net = ipaddress.ip_network(f"{iface.ip}/24", strict=False)
        nets.append((iface.ip, net))
    if not nets:
        # Ask the OS which address it would use to reach the outside world. UDP connect
        # sends nothing, it just picks a route. Assume a /24 around it.
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("10.255.255.255", 1))
                ip = ipaddress.ip_address(s.getsockname()[0])
            if not ip.is_loopback:
                nets.append((ip, ipaddress.ip_network(f"{ip}/24", strict=False)))
        except OSError:
            pass
    return nets


def arp_neighbors():
    try:
        out = subprocess.run(["ip", "-4", "neigh"], capture_output=True, text=True).stdout
        return [l.split()[0] for l in out.splitlines() if l and "FAILED" not in l]
    except Exception:
        pass
    try:  # macOS / Windows
        out = subprocess.run(["arp", "-a"], capture_output=True, text=True).stdout
        return re.findall(r"\b(\d+\.\d+\.\d+\.\d+)\b", out)
    except Exception:
        return []


def port_open(ip, port, timeout=0.6):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def looks_like_tapo(ip):
    # Tapo cameras expose RTSP on 554 and ONVIF on 2020 - that pair is a strong signature.
    return port_open(ip, RTSP_PORT) and port_open(ip, ONVIF_PORT)


def ws_discovery(timeout=2.0):
    """ONVIF WS-Discovery multicast probe. Returns IPs that answered."""
    msg = f"""<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
<e:Header><w:MessageID>uuid:{secrets.token_hex(16)}</w:MessageID><w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
<w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>
<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>"""
    found = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        s.settimeout(0.3)
        s.sendto(msg.encode(), ("239.255.255.250", 3702))
        end = time.time() + timeout
        while time.time() < end:
            try:
                _, addr = s.recvfrom(65535)
                found.add(addr[0])
            except socket.timeout:
                pass
        s.close()
    except OSError:
        pass
    return found


class Camera:
    def __init__(self, ip):
        self.ip = ip
        known = CONFIG["known"].get(ip, {})
        self.name = known.get("name") or f"Camera {ip}"
        self.model = known.get("model", "")
        self.online = True
        self.last_seen = time.time()
        self.error = ""
        self.onvif = Onvif(ip)
        self._tapo = None
        self._tapo_err = ""
        self._tapo_fail_at = 0
        self.recorder = None
        self.record_file = ""

    # ---- pytapo (advanced controls; needs the Tapo app / cloud account password)
    def tapo(self):
        if Tapo is None:
            raise RuntimeError("pytapo not installed")
        if not CONFIG["cloud_password"]:
            raise RuntimeError("Enter your Tapo app password in Settings to enable this control")
        if self._tapo is None:
            # A failed login is not retried automatically: repeated bad attempts make the
            # camera lock the account out. Saving Settings clears this.
            if self._tapo_fail_at:
                raise RuntimeError(f"Tapo login failed: {self._tapo_err} (fix the Tapo app password in Settings)")
            try:
                self._tapo = Tapo(self.ip, "admin", CONFIG["cloud_password"], CONFIG["cloud_password"],
                                  printWarnInformation=False)
            except Exception as e:
                self._tapo_err = str(e)
                self._tapo_fail_at = time.time()
                raise RuntimeError(f"Tapo login failed: {e}")
        return self._tapo

    def reset_sessions(self):
        self._tapo = None
        self._tapo_fail_at = 0
        self.onvif = Onvif(self.ip)

    def info(self):
        return {"ip": self.ip, "name": self.name, "model": self.model, "online": self.online,
                "error": self.error, "recording": self.recorder is not None and self.recorder.poll() is None,
                "record_file": self.record_file}


CAMERAS = {}  # ip -> Camera
STATE = {"scanning": False, "last_scan": 0, "networks": [], "scan_note": ""}


def identify(cam):
    """Best effort: fill in a friendly name/model."""
    try:
        if CONFIG["cloud_password"]:
            info = cam.tapo().getBasicInfo()
            bi = info.get("device_info", {}).get("basic_info", info)
            cam.name = bi.get("device_alias") or cam.name
            cam.model = bi.get("device_model") or cam.model
        elif have_creds():
            d = cam.onvif.device_info()
            cam.model = d.get("Model", cam.model)
            if cam.name.startswith("Camera "):
                cam.name = f"{d.get('Manufacturer', 'Tapo')} {cam.model} ({cam.ip})"
    except Exception:
        pass
    CONFIG["known"][cam.ip] = {"name": cam.name, "model": cam.model}
    save_config()


def scan_once():
    nets = local_networks()
    STATE["networks"] = [str(n) for _, n in nets]
    if not nets:
        STATE["scan_note"] = "Not connected to any network yet - waiting..."
        return []
    STATE["scan_note"] = "Scanning " + ", ".join(STATE["networks"]) + "..."
    own = {str(ip) for ip, _ in nets}
    candidates = []
    for ip in list(CONFIG["manual_ips"]) + list(CONFIG["known"]) + list(ws_discovery()) + arp_neighbors():
        if ip not in candidates and ip not in own:
            candidates.append(ip)
    for _, net in nets:
        for h in net.hosts():
            s = str(h)
            if s not in candidates and s not in own:
                candidates.append(s)
    with ThreadPoolExecutor(max_workers=128) as pool:
        hits = [ip for ip, ok in zip(candidates, pool.map(looks_like_tapo, candidates)) if ok]
    return hits


def discovery_loop():
    while True:
        STATE["scanning"] = True
        try:
            hits = set(scan_once())
        except Exception as e:
            hits = set()
            STATE["scan_note"] = f"Scan error: {e}"
        now = time.time()
        with LOCK:
            for ip in hits:
                if ip not in CAMERAS:
                    cam = CAMERAS[ip] = Camera(ip)
                    threading.Thread(target=identify, args=(cam,), daemon=True).start()
                CAMERAS[ip].online = True
                CAMERAS[ip].last_seen = now
            for ip, cam in CAMERAS.items():
                if ip not in hits and not port_open(ip, RTSP_PORT, 1.5):
                    cam.online = False
        STATE["scanning"] = False
        STATE["last_scan"] = now
        if hits:
            STATE["scan_note"] = f"Found {len(hits)} camera(s)."
        elif STATE["networks"]:
            STATE["scan_note"] = "No Tapo camera found yet on " + ", ".join(STATE["networks"]) + " - still looking..."
        # rescan quickly until something is found, then relax
        wait = 60 if any(c.online for c in CAMERAS.values()) else 5
        for _ in range(wait):
            time.sleep(1)
            if STATE.get("rescan_now"):
                STATE["rescan_now"] = False
                break


# ---------------------------------------------------------------- ONVIF (PTZ + presets) with the camera account

class Onvif:
    def __init__(self, ip):
        self.url = f"http://{ip}:{ONVIF_PORT}/onvif/service"
        self.offset = None
        self.profile = None

    def _post(self, body, auth=True):
        header = ""
        if auth:
            if self.offset is None:
                self.offset = self._clock_offset()
            created = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=self.offset)) \
                .strftime("%Y-%m-%dT%H:%M:%S.000Z")
            nonce = secrets.token_bytes(16)
            digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + CONFIG["password"].encode()).digest()).decode()
            header = f"""<s:Header><Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
<UsernameToken><Username>{_xml(CONFIG['username'])}</Username>
<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</Password>
<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{base64.b64encode(nonce).decode()}</Nonce>
<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>
</UsernameToken></Security></s:Header>"""
        env = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"
 xmlns:tt="http://www.onvif.org/ver10/schema" xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
 xmlns:tds="http://www.onvif.org/ver10/device/wsdl">{header}<s:Body>{body}</s:Body></s:Envelope>"""
        req = urllib.request.Request(self.url, data=env.encode(),
                                     headers={"Content-Type": "application/soap+xml; charset=utf-8"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            reason = re.search(r"<[^>]*Text[^>]*>([^<]+)<", text)
            raise RuntimeError(f"ONVIF error {e.code}: {reason.group(1) if reason else text[:200]}")

    def _clock_offset(self):
        try:
            x = self._post("<tds:GetSystemDateAndTime/>", auth=False)
            utc = x[x.find("UTCDateTime"):]
            g = lambda t: int(re.search(rf"<[^>]*{t}>(\d+)<", utc).group(1))
            cam = datetime.datetime(g("Year"), g("Month"), g("Day"), g("Hour"), g("Minute"), g("Second"),
                                    tzinfo=datetime.timezone.utc)
            return (cam - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        except Exception:
            return 0

    def device_info(self):
        x = self._post("<tds:GetDeviceInformation/>")
        return {k: v for k, v in re.findall(r"<[^>:]*:?(Manufacturer|Model|FirmwareVersion|SerialNumber)>([^<]*)<", x)}

    def _profile(self):
        if not self.profile:
            x = self._post("<trt:GetProfiles/>")
            m = re.search(r'Profiles[^>]*token="([^"]+)"', x)
            self.profile = m.group(1) if m else "profile_1"
        return self.profile

    def move(self, x, y):
        self._post(f'<tptz:ContinuousMove><tptz:ProfileToken>{self._profile()}</tptz:ProfileToken>'
                   f'<tptz:Velocity><tt:PanTilt x="{x}" y="{y}"/></tptz:Velocity></tptz:ContinuousMove>')

    def step(self, x, y):
        self._post(f'<tptz:RelativeMove><tptz:ProfileToken>{self._profile()}</tptz:ProfileToken>'
                   f'<tptz:Translation><tt:PanTilt x="{x}" y="{y}"/></tptz:Translation></tptz:RelativeMove>')

    def stop(self):
        self._post(f'<tptz:Stop><tptz:ProfileToken>{self._profile()}</tptz:ProfileToken>'
                   f'<tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom></tptz:Stop>')

    def presets(self):
        x = self._post(f"<tptz:GetPresets><tptz:ProfileToken>{self._profile()}</tptz:ProfileToken></tptz:GetPresets>")
        out = []
        for tok, inner in re.findall(r'Preset[^>]*token="([^"]+)"[^>]*>(.*?)</[^>]*Preset>', x, re.S):
            n = re.search(r"Name>([^<]*)<", inner)
            out.append({"id": tok, "name": n.group(1) if n else tok})
        return out

    def goto_preset(self, token):
        self._post(f"<tptz:GotoPreset><tptz:ProfileToken>{self._profile()}</tptz:ProfileToken>"
                   f"<tptz:PresetToken>{_xml(token)}</tptz:PresetToken></tptz:GotoPreset>")

    def save_preset(self, name):
        self._post(f"<tptz:SetPreset><tptz:ProfileToken>{self._profile()}</tptz:ProfileToken>"
                   f"<tptz:PresetName>{_xml(name)}</tptz:PresetName></tptz:SetPreset>")

    def remove_preset(self, token):
        self._post(f"<tptz:RemovePreset><tptz:ProfileToken>{self._profile()}</tptz:ProfileToken>"
                   f"<tptz:PresetToken>{_xml(token)}</tptz:PresetToken></tptz:RemovePreset>")


def _xml(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ---------------------------------------------------------------- RTSP relay
#
# ffmpeg only takes RTSP credentials inside the URL on its command line, and any user on the
# machine can read another process's command line. So ffmpeg never gets the login: it connects
# to a one-shot relay on 127.0.0.1 and the relay answers the camera's auth challenge itself.
# The relay only serves a connection the OS confirms belongs to the ffmpeg process we started.

def spawn_ffmpeg(cam, quality, pre, post, **popen_kw):
    lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(8)
    url = f"rtsp://127.0.0.1:{lsock.getsockname()[1]}/{'stream1' if quality == 'hd' else 'stream2'}"
    proc = subprocess.Popen([FFMPEG, *pre, "-rtsp_transport", "tcp", "-i", url, *post], **popen_kw)
    threading.Thread(target=_relay_accept, args=(lsock, proc, cam), daemon=True).start()
    return proc


def _owned_by(pid, peer, local):
    for _ in range(20):  # the connection can take a moment to show up in the process's table
        try:
            for c in psutil.Process(pid).net_connections(kind="tcp4"):
                if c.laddr and c.raddr and tuple(c.laddr) == peer and tuple(c.raddr) == local:
                    return True
        except psutil.Error:
            return False
        time.sleep(0.05)
    return False


def _relay_accept(lsock, proc, cam):
    lsock.settimeout(1)
    local = lsock.getsockname()
    try:
        while proc.poll() is None:
            try:
                conn, peer = lsock.accept()
            except socket.timeout:
                continue
            if not _owned_by(proc.pid, peer, local):
                debug(f"relay: rejected connection from {peer} (not our ffmpeg, pid {proc.pid})")
                conn.close()  # someone else on this machine - not our ffmpeg
                continue
            try:
                RtspRelay(conn, cam)
            except OSError as e:
                debug(f"relay: can't reach camera {cam.ip}:{RTSP_PORT}: {e}")
                conn.close()
            return  # ffmpeg only opens one control connection
    finally:
        lsock.close()


def _read_rtsp(f):
    """Next message from a buffered socket file: bytes for an interleaved ($) data frame,
    otherwise (start_line, [(name, value)], body). None on EOF."""
    first = f.read(1)
    if not first:
        return None
    if first == b"$":
        hdr = f.read(3)
        return first + hdr + f.read(int.from_bytes(hdr[1:3], "big"))
    start = (first + f.readline()).decode("utf-8", "replace").strip()
    headers = []
    while True:
        line = f.readline()
        if not line:
            return None
        line = line.decode("utf-8", "replace").strip()
        if not line:
            break
        name, _, value = line.partition(":")
        headers.append((name.strip(), value.strip()))
    n = next((int(v) for k, v in headers if k.lower() == "content-length"), 0)
    return start, headers, f.read(n) if n else b""


def _build_rtsp(start, headers, body):
    lines = [start] + [f"{k}: {v}" for k, v in headers]
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


class RtspRelay:
    def __init__(self, client, cam):
        self.client = client
        self.base = f"rtsp://{cam.ip}:{RTSP_PORT}"
        self.cam = socket.create_connection((cam.ip, RTSP_PORT), timeout=10)
        self.cam.settimeout(None)
        self.lock = threading.Lock()
        self.pending = collections.deque()  # requests waiting for the camera's reply
        self.challenge = None
        self.nc = 0
        threading.Thread(target=self._from_client, daemon=True).start()
        threading.Thread(target=self._from_camera, daemon=True).start()

    def _close(self):
        for s in (self.client, self.cam):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()

    def _auth(self, method, uri):
        ch, user, pw = self.challenge, CONFIG["username"], CONFIG["password"]
        if ch["scheme"] == "basic":
            return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
        algo = ch.get("algorithm", "MD5")
        H = hashlib.sha256 if algo.upper().startswith("SHA-256") else hashlib.md5
        h = lambda x: H(x.encode()).hexdigest()
        ha1, ha2 = h(f"{user}:{ch.get('realm', '')}:{pw}"), h(f"{method}:{uri}")
        fields = {"username": user, "realm": ch.get("realm", ""), "nonce": ch.get("nonce", ""), "uri": uri}
        if "auth" in ch.get("qop", "").split(","):
            self.nc += 1
            nc, cnonce = f"{self.nc:08x}", secrets.token_hex(8)
            fields["response"] = h(f"{ha1}:{fields['nonce']}:{nc}:{cnonce}:auth:{ha2}")
            extra = f', qop=auth, nc={nc}, cnonce="{cnonce}"'
        else:
            fields["response"] = h(f"{ha1}:{fields['nonce']}:{ha2}")
            extra = ""
        if "opaque" in ch:
            fields["opaque"] = ch["opaque"]
        if "algorithm" in ch:
            extra += f", algorithm={algo}"
        return "Digest " + ", ".join(f'{k}="{v}"' for k, v in fields.items()) + extra

    def _send(self, req):
        method, uri, ver, headers, body = req["method"], req["uri"], req["ver"], req["headers"], req["body"]
        if self.challenge:
            headers = headers + [("Authorization", self._auth(method, uri))]
        debug(f"relay -> camera: {method} {uri}", f"(auth: {self.challenge['scheme']})" if self.challenge else "(no auth)")
        with self.lock:
            self.cam.sendall(_build_rtsp(f"{method} {uri} {ver}", headers, body))

    def _from_client(self):
        f = self.client.makefile("rb")
        try:
            while (msg := _read_rtsp(f)) is not None:
                if isinstance(msg, bytes):
                    with self.lock:
                        self.cam.sendall(msg)
                    continue
                start, headers, body = msg
                method, uri, ver = (start.split(" ", 2) + ["", ""])[:3]
                req = {"method": method, "uri": re.sub(r"^rtsp://[^/]+", self.base, uri), "ver": ver,
                       "headers": [(k, v) for k, v in headers if k.lower() != "authorization"],
                       "body": body, "retried": False}
                self.pending.append(req)
                self._send(req)
        except OSError:
            pass
        finally:
            self._close()

    def _from_camera(self):
        f = self.cam.makefile("rb")
        try:
            while (msg := _read_rtsp(f)) is not None:
                if isinstance(msg, bytes):
                    self.client.sendall(msg)
                    continue
                start, headers, body = msg
                req = self.pending.popleft() if self.pending else None
                debug(f"camera -> relay: {start}", [v for k, v in headers if k.lower() == "www-authenticate"] or "")
                if " 401 " in f"{start} " and req and not req["retried"]:
                    offers = [v for k, v in headers if k.lower() == "www-authenticate"]
                    offer = next((o for o in offers if o.lower().startswith("digest")), offers[0] if offers else "")
                    if offer:
                        self.challenge = {k.lower(): a or b for k, a, b in
                                          re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]*))', offer)}
                        self.challenge["scheme"] = offer.split()[0].lower()
                        req["retried"] = True
                        self.pending.appendleft(req)
                        self._send(req)
                        continue
                if " 401 " in f"{start} ":
                    # our login was refused. Pass the 401 on without the challenge so ffmpeg gives up
                    # instead of retrying - every extra bad attempt counts toward the camera's lockout.
                    headers = [(k, v) for k, v in headers if k.lower() != "www-authenticate"]
                self.client.sendall(_build_rtsp(start, headers, body))
        except OSError:
            pass
        finally:
            self._close()


# ---------------------------------------------------------------- video: RTSP -> MJPEG

class Stream:
    """One ffmpeg per (camera, quality); fans JPEG frames out to any number of browser clients."""

    def __init__(self, cam, quality):
        self.cam, self.quality = cam, quality
        self.frame = None
        self.seq = 0
        self.cond = threading.Condition()
        self.clients = 0
        self.idle_since = time.time()
        self.proc = None
        self.alive = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while self.alive:
            if not have_creds():
                self.cam.error = "Enter the camera account username/password in Settings"
                time.sleep(2)
                continue
            self.proc = spawn_ffmpeg(
                self.cam, self.quality,
                ["-hide_banner", "-loglevel", "error", "-fflags", "nobuffer", "-flags", "low_delay", "-timeout", "5000000"],
                ["-an", "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "4" if self.quality == "hd" else "6", "-"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            u = CONFIG["username"]
            debug(f"stream {self.cam.ip} {self.quality}: username={u!r}, password is {len(CONFIG['password'])} chars"
                  + (" (has leading/trailing spaces!)" if CONFIG["password"] != CONFIG["password"].strip() else ""))
            errbuf = []
            threading.Thread(target=lambda: errbuf.extend(self.proc.stderr.read().decode("utf-8", "replace").splitlines()),
                             daemon=True).start()
            buf = b""
            got_frame = False
            while self.alive:
                chunk = self.proc.stdout.read(65536)
                if not chunk:
                    break
                buf += chunk
                while True:
                    s = buf.find(b"\xff\xd8")
                    e = buf.find(b"\xff\xd9", s + 2) if s >= 0 else -1
                    if s < 0 or e < 0:
                        if s > 0:
                            buf = buf[s:]
                        break
                    jpg, buf = buf[s:e + 2], buf[e + 2:]
                    if not got_frame:
                        got_frame = True
                        self.cam.error = ""
                    with self.cond:
                        self.frame = jpg
                        self.seq += 1
                        self.cond.notify_all()
            self._kill()
            if not self.alive:
                break
            err = " ".join(errbuf)
            if err:
                debug(f"ffmpeg ({self.cam.ip} {self.quality}): {err}")
            if "Unauthorized" in err:
                self.cam.error = ("Camera rejected the login. The Camera Account username and password are case-sensitive, "
                                  "so check them against the Tapo app, and make sure Third-Party Compatibility is ON "
                                  "(Tapo app > Me > Tapo Lab)")
                # back off: hammering the camera with a bad login can get this computer locked out.
                # Saving Settings starts a fresh stream right away.
                time.sleep(30)
            else:
                self.cam.error = "" if got_frame else ("Stream error: " + err[-200:] if err else "Connecting to stream...")
                time.sleep(2)

    def _kill(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.wait(3)
            except Exception:
                pass

    def stop(self):
        self.alive = False
        self._kill()
        with self.cond:
            self.cond.notify_all()


STREAMS = {}


def get_stream(cam, quality):
    key = (cam.ip, quality)
    with LOCK:
        st = STREAMS.get(key)
        if st is None or not st.alive:
            st = STREAMS[key] = Stream(cam, quality)
        return st


def restart_streams():
    with LOCK:
        for st in STREAMS.values():
            st.stop()
        STREAMS.clear()


def reaper():
    """Stop ffmpeg for streams nobody has watched for a while."""
    while True:
        time.sleep(5)
        with LOCK:
            for key, st in list(STREAMS.items()):
                if st.clients == 0 and time.time() - st.idle_since > 20:
                    st.stop()
                    del STREAMS[key]


# ---------------------------------------------------------------- recording

def start_recording(cam):
    if cam.recorder and cam.recorder.poll() is None:
        return
    os.makedirs(RECORD_DIR, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", cam.name).strip("_") or cam.ip
    cam.record_file = os.path.join(RECORD_DIR, f"{safe}_{time.strftime('%Y-%m-%d_%H-%M-%S')}.mkv")
    # stream copy: no re-encode, full camera quality incl. audio. MKV survives an abrupt stop.
    cam.recorder = spawn_ffmpeg(cam, "hd", ["-hide_banner", "-loglevel", "error"],
                                ["-map", "0", "-c", "copy", cam.record_file],
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop_recording(cam):
    if cam.recorder and cam.recorder.poll() is None:
        try:
            cam.recorder.stdin.write(b"q")
            cam.recorder.stdin.flush()
            cam.recorder.wait(5)
        except Exception:
            cam.recorder.kill()
    cam.recorder = None


# ---------------------------------------------------------------- advanced controls (pytapo)

def tapo_status(cam):
    t = cam.tapo()
    out = {}
    probes = {
        "privacy": lambda: t.getPrivacyMode().get("enabled") == "on",
        "night_vision": lambda: t.getDayNightMode(),
        "led": lambda: t.getLED().get("enabled") == "on",
        "motion": lambda: t.getMotionDetection().get("enabled") == "on",
        "person": lambda: t.getPersonDetection().get("enabled") == "on",
        "alarm": lambda: t.getAlarm().get("enabled") == "on",
        "flip": lambda: bool(t.getImageFlipVertical()),
        "autotrack": lambda: t.getAutoTrackTarget().get("enabled") == "on",
        "lens_correction": lambda: bool(t.getLensDistortionCorrection()),
    }
    for k, fn in probes.items():
        try:
            out[k] = fn()
        except Exception:
            pass  # not supported by this model
    return out


def tapo_action(cam, action, value):
    t = cam.tapo()
    on = value in (True, "on", "true", 1, "1")
    actions = {
        "privacy": lambda: t.setPrivacyMode(on),
        "night_vision": lambda: t.setDayNightMode(value),
        "led": lambda: t.setLEDEnabled(on),
        "motion": lambda: t.setMotionDetection(on),
        "person": lambda: t.setPersonDetection(on),
        "alarm": lambda: t.setAlarm(on),
        "siren": lambda: t.startManualAlarm() if on else t.stopManualAlarm(),
        "flip": lambda: t.setImageFlipVertical(on),
        "autotrack": lambda: t.setAutoTrackTarget(on),
        "lens_correction": lambda: t.setLensDistortionCorrection(on),
        "calibrate": lambda: t.calibrateMotor(),
        "reboot": lambda: t.reboot(),
    }
    if action not in actions:
        raise RuntimeError(f"Unknown action {action}")
    return actions[action]()


def ptz(cam, direction, mode):
    vec = {"up": (0, 1), "down": (0, -1), "left": (-1, 0), "right": (1, 0),
           "upleft": (-1, 1), "upright": (1, 1), "downleft": (-1, -1), "downright": (1, -1)}
    if mode == "stop":
        try:
            cam.onvif.stop()
        except Exception:
            pass
        return
    x, y = vec[direction]
    try:
        if mode == "start":
            cam.onvif.move(x * 0.6, y * 0.6)
        else:
            cam.onvif.step(x * 0.1, y * 0.1)
    except Exception as onvif_err:
        # fall back to Tapo's native motor API
        try:
            cam.tapo().moveMotor(x * 10, y * 10)
        except Exception:
            raise onvif_err


def list_presets(cam):
    try:
        return cam.onvif.presets()
    except Exception:
        p = cam.tapo().getPresets()
        return [{"id": k, "name": v} for k, v in p.items()]


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _allowed(self):
        # Only answer requests addressed to us by name (stops DNS rebinding), and make
        # POSTs carry a JSON content type so other websites can't fire them blind.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host not in ("127.0.0.1", "localhost", "[::1]") and host != HOST:
            self._json({"error": "forbidden"}, 403)
            return False
        # Browsers label cross-site requests; refuse those so other pages can't embed the feed.
        if self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
            self._json({"error": "forbidden"}, 403)
            return False
        if self.command == "POST" and "application/json" not in (self.headers.get("Content-Type") or ""):
            self._json({"error": "expected application/json"}, 415)
            return False
        return True

    def _cam(self, q):
        ip = (q.get("ip") or [""])[0]
        cam = CAMERAS.get(ip)
        if not cam:
            raise KeyError("camera not found")
        return cam

    def do_GET(self):
        if not self._allowed():
            return
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(APP_DIR, "index.html"), "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/state":
                self._json({"cameras": [c.info() for c in CAMERAS.values()], "scan": STATE,
                            "has_creds": have_creds(), "has_cloud": bool(CONFIG["cloud_password"]),
                            "username": CONFIG["username"], "manual_ips": CONFIG["manual_ips"],
                            "keychain": KEYCHAIN is not None,
                            "record_dir": RECORD_DIR})
            elif u.path == "/api/status":
                self._json(tapo_status(self._cam(q)))
            elif u.path == "/api/presets":
                self._json(list_presets(self._cam(q)))
            elif u.path == "/video":
                self._video(self._cam(q), (q.get("q") or ["hd"])[0])
            elif u.path == "/snapshot":
                cam = self._cam(q)
                st = get_stream(cam, "hd")
                with st.cond:
                    st.cond.wait_for(lambda: st.frame is not None or not st.alive, timeout=10)
                    frame = st.frame
                if not frame:
                    return self._json({"error": "no frame yet"}, 503)
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="snapshot_{time.strftime("%Y-%m-%d_%H-%M-%S")}.jpg"')
                self.send_header("Content-Length", str(len(frame)))
                self.end_headers()
                self.wfile.write(frame)
            elif u.path == "/audio":
                self._audio(self._cam(q))
            else:
                self._json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._json({"error": str(e)}, 500)
            except Exception:
                pass

    def do_POST(self):
        if not self._allowed():
            return
        u = urllib.parse.urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n) or b"{}")
            q = {"ip": [data.get("ip", "")]}
            if u.path == "/api/settings":
                for k in ("username", "password", "cloud_password"):
                    if k in data and (data[k] or k == "username"):
                        CONFIG[k] = data[k]
                if "manual_ips" in data:
                    CONFIG["manual_ips"] = [s.strip() for s in data["manual_ips"] if s.strip()]
                save_config()
                for c in CAMERAS.values():
                    c.reset_sessions()
                    threading.Thread(target=identify, args=(c,), daemon=True).start()
                restart_streams()
                STATE["rescan_now"] = True
                self._json({"ok": True})
            elif u.path == "/api/quit":
                self._json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            elif u.path == "/api/rescan":
                STATE["rescan_now"] = True
                self._json({"ok": True})
            elif u.path == "/api/rename":
                cam = self._cam(q)
                cam.name = data.get("name") or cam.name
                CONFIG["known"][cam.ip] = {"name": cam.name, "model": cam.model}
                save_config()
                self._json({"ok": True})
            elif u.path == "/api/ptz":
                ptz(self._cam(q), data.get("dir", ""), data.get("mode", "step"))
                self._json({"ok": True})
            elif u.path == "/api/preset":
                cam = self._cam(q)
                op = data.get("op")
                try:
                    if op == "goto":
                        cam.onvif.goto_preset(data["id"])
                    elif op == "save":
                        cam.onvif.save_preset(data["name"])
                    elif op == "delete":
                        cam.onvif.remove_preset(data["id"])
                except Exception:
                    t = cam.tapo()
                    {"goto": lambda: t.setPreset(data["id"]), "save": lambda: t.savePreset(data["name"]),
                     "delete": lambda: t.deletePreset(data["id"])}[op]()
                self._json({"ok": True})
            elif u.path == "/api/control":
                res = tapo_action(self._cam(q), data.get("action"), data.get("value"))
                self._json({"ok": True, "result": res if isinstance(res, (dict, list, str, int)) else None})
            elif u.path == "/api/record":
                cam = self._cam(q)
                (start_recording if data.get("on") else stop_recording)(cam)
                self._json({"ok": True, "file": cam.record_file})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _video(self, cam, quality):
        st = get_stream(cam, "sd" if quality == "sd" else "hd")
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        with LOCK:
            st.clients += 1
        last = -1
        try:
            while st.alive:
                with st.cond:
                    st.cond.wait_for(lambda: st.seq != last or not st.alive, timeout=15)
                    frame, last = st.frame, st.seq
                if frame is None or not st.alive:
                    continue
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                 + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
        finally:
            with LOCK:
                st.clients -= 1
                st.idle_since = time.time()

    def _audio(self, cam):
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        p = spawn_ffmpeg(cam, "sd", ["-hide_banner", "-loglevel", "error"],
                         ["-vn", "-c:a", "libmp3lame", "-b:a", "64k", "-flush_packets", "1", "-f", "mp3", "-"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        try:
            while True:
                chunk = p.stdout.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
        finally:
            p.kill()


# ---------------------------------------------------------------- main

class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            super().handle_error(request, client_address)  # browser closing a tab isn't worth a traceback


def main():
    global PORT, DEBUG
    ap = argparse.ArgumentParser(description="Local viewer for TP-Link Tapo cameras.")
    ap.add_argument("--port", type=int, default=PORT, help=f"web UI port (default {PORT})")
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser tab on start")
    ap.add_argument("--debug", action="store_true", help="print what's happening (never prints passwords)")
    ap.add_argument("--ip", action="append", default=[], metavar="ADDR",
                    help="camera IP to check even if discovery misses it (repeatable)")
    args = ap.parse_args()
    DEBUG = args.debug
    debug(f"keychain: {type(KEYCHAIN).__name__ if KEYCHAIN else 'none (using config file)'}, ffmpeg: {FFMPEG}")
    PORT = args.port
    for ip in args.ip:
        if ip not in CONFIG["manual_ips"]:
            CONFIG["manual_ips"].append(ip)

    url = f"http://{HOST}:{PORT}/"
    try:
        server = Server((HOST, PORT), Handler)
    except OSError:
        # already running - just bring the page up
        print(f"already running at {url}")
        webbrowser.open(url)
        return
    threading.Thread(target=discovery_loop, daemon=True).start()
    threading.Thread(target=reaper, daemon=True).start()
    print(f"tapo-camera-viewer running at {url}  (Ctrl+C to quit)")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for c in CAMERAS.values():
            stop_recording(c)
        restart_streams()


if __name__ == "__main__":
    main()
