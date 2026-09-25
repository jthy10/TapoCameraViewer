# Tapo Camera Viewer

A small local viewer for TP-Link Tapo cameras. It runs on your computer, finds the cameras on your network and gives you a live feed and controls in the browser. No cloud and no Tapo account login over the internet: everything stays on your LAN.

What it does:

- finds Tapo cameras on your network automatically (or you can give it IPs)
- live video (HD or SD stream), fullscreen, snapshots
- listen to camera audio
- record to `.mkv` (straight copy of the camera stream, no re-encoding)
- pan/tilt with on-screen buttons or arrow keys, plus presets
- privacy mode, night vision, LED, motion/person detection, auto-track, siren, reboot, etc. (needs your Tapo app password, see below)
- handles multiple cameras

Tested with a Tapo C113 on Ubuntu 24.04. Other Tapo models that support RTSP/ONVIF (C100, C200, C210, C220, C310, TC70...) should work. Some controls only show up if your model supports them.

## Camera setup (do this first)

You need to do two things in the Tapo app. **If you skip step 2, the camera rejects every login and you'll only get 401 errors.**

1. **Create a Camera Account**
   Tapo app → tap your camera → ⚙ (settings) → Advanced Settings → **Camera Account**.
   Pick a username and password. This is *not* your Tapo/TP-Link login, it's a separate local account for the camera.

2. **Turn on Third-Party Compatibility**
   Tapo app → **Me** (bottom right) → **Tapo Lab** → **Third-Party Compatibility** → **On**.
   Newer firmware blocks RTSP/ONVIF unless this is on.

Your computer also needs to be on the same network as the camera.

## Install

### Windows

1. Download `TapoCameraViewer-windows.exe` from the [latest release](https://github.com/jthy10/TapoCameraViewer/releases/latest).
2. Double-click it.

That's it, nothing else to install. Windows will probably show a blue "Windows protected your PC" box the first time, because the exe isn't code-signed (certificates cost money). Click **More info → Run anyway**.

A console window stays open while it runs. Close it (or hit Quit in the web page) to stop.

### Linux

Either grab `TapoCameraViewer-linux` from the [latest release](https://github.com/jthy10/TapoCameraViewer/releases/latest) and run it:

```bash
chmod +x TapoCameraViewer-linux
./TapoCameraViewer-linux
```

or use the install script, which adds a `tapo-camera-viewer` command and an app menu entry (run it again to update):

```bash
curl -fsSL https://raw.githubusercontent.com/jthy10/TapoCameraViewer/main/install.sh | bash
```

### macOS

No prebuilt app yet. Use the install script (needs Python 3.9+, which you can get from [python.org](https://www.python.org/downloads/) if you don't have it):

```bash
curl -fsSL https://raw.githubusercontent.com/jthy10/TapoCameraViewer/main/install.sh | bash
```

### From source

```bash
git clone https://github.com/jthy10/TapoCameraViewer.git
cd TapoCameraViewer
./install.sh        # Windows: install.bat
```

## Usage

Start it (double-click the exe, the app menu entry, or `tapo-camera-viewer`). It opens http://127.0.0.1:8765 in your browser. The first time, it asks for:

- **Camera account username/password**: the one from step 1. Required.
- **Tapo app password** (optional): your normal TP-Link ID password. Only needed for privacy mode, night vision, LED, detection toggles, siren and reboot. Video and pan/tilt work without it.
- **Camera IPs** (optional): if auto-discovery doesn't find your camera, put its IP here. You can find it in the Tapo app under camera settings → Device Info.

It keeps scanning in the background, so a camera that's unplugged or rebooting shows up again on its own.

Command line options (they work with the exe too):

```
tapo-camera-viewer --port 9000         # use a different port
tapo-camera-viewer --no-browser        # don't open a browser tab
tapo-camera-viewer --ip 192.168.1.50   # always check this IP (repeatable)
```

Running it again while it's already running just opens the page.

Pan/tilt: click an arrow to nudge, hold it to keep moving. Arrow keys work too. The middle button recalibrates the motor.

Passwords go in your system keychain (GNOME Keyring / KWallet on Linux, Keychain on macOS, Credential Manager on Windows). Everything else is in `~/.config/tapo-camera-viewer/config.json`. Recordings go to `~/Videos/tapo-camera-viewer/`.

## Troubleshooting

**"Camera rejected the login" / 401 errors**
Nine times out of ten, Third-Party Compatibility is off. Turn it on (see setup step 2). Then double-check the Camera Account username/password. A factory reset or firmware update can switch it off again.

**Camera not found**
Make sure you're on the same network/subnet as the camera. Some routers block multicast and client-to-client traffic (guest networks, "AP isolation"). If so, add the camera's IP in Settings.

**Advanced controls say "login failed" or the camera is locked out**
That's the Tapo app password. If you enter it wrong a few times, the camera temporarily suspends logins for a while. This app doesn't retry a failed login automatically for this reason. Fix the password in Settings and wait it out if you're locked.

**Video is laggy**
Switch to SD in the quality dropdown. HD is the full main stream and can be heavy over Wi-Fi.

## Uninstall

```bash
rm -rf ~/.local/share/tapo-camera-viewer ~/.local/bin/tapo-camera-viewer ~/.local/share/applications/tapo-camera-viewer.desktop ~/.config/tapo-camera-viewer
```

Saved passwords stay in your system keychain. Delete the `tapo-camera-viewer` entries from your keychain app if you want those gone too.

## Notes

The server only listens on 127.0.0.1, so other devices on your network can't reach it, and it refuses requests coming from other websites. There's no login on the web UI though, so anyone with an account on the same computer can open it. Don't expose it to other machines.

Passwords never touch disk in plain text as long as you have a keychain. If you don't (some minimal Linux setups), it falls back to the config file and Settings tells you so. They also never show up in the process list: ffmpeg connects to a small relay inside the app, which handles the camera login, and the relay only accepts connections from the ffmpeg process it started.

Not affiliated with TP-Link. "Tapo" is their trademark.

## Contact

- Website: [jrtiv.com](https://jrtiv.com)
- Twitter/X: [@JakeThygeson](https://x.com/JakeThygeson)

MIT license.
