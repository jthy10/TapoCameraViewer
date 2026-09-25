#!/bin/bash
# Tapo Camera Viewer installer (Linux / macOS).
#
#   curl -fsSL https://raw.githubusercontent.com/jthy10/tapo-camera-viewer/main/install.sh | bash
#
# or from a cloned repo:  ./install.sh
#
# Re-run it any time to update.
set -e

REPO="jthy10/tapo-camera-viewer"
APP="tapo-camera-viewer"

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# --- where are we installing from?
SRC="${BASH_SOURCE[0]:-}"
if [ -n "$SRC" ] && [ -f "$(dirname "$SRC")/tapo_camera_viewer.py" ]; then
    DIR="$(cd "$(dirname "$SRC")" && pwd)"
else
    # piped from curl: download the latest code
    DIR="$HOME/.local/share/$APP"
    say "Downloading to $DIR"
    command -v curl >/dev/null || die "curl is required"
    mkdir -p "$DIR"
    curl -fsSL "https://github.com/$REPO/archive/refs/heads/main.tar.gz" | tar -xz -C "$DIR" --strip-components=1
fi
cd "$DIR"

# --- python
if ! command -v python3 >/dev/null; then
    if command -v apt-get >/dev/null; then
        say "Installing python3 (needs sudo)"
        sudo apt-get update -q && sudo apt-get install -y python3
    else
        die "python3 not found. Install Python 3.9+ (https://www.python.org/downloads/) and run this again."
    fi
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "Python 3.9 or newer is required"

# --- venv (with a fallback for Debian/Ubuntu machines missing python3-venv)
if ! ./venv/bin/python -c 'import pip' 2>/dev/null; then
    say "Creating virtualenv"
    rm -rf venv
    if ! python3 -m venv venv >/dev/null 2>&1; then
        rm -rf venv
        python3 -m venv --without-pip venv || die "couldn't create a virtualenv"
        say "Bootstrapping pip"
        curl -fsSL https://bootstrap.pypa.io/get-pip.py | ./venv/bin/python - -q
    fi
fi

say "Installing dependencies"
./venv/bin/python -m pip install -q --upgrade pip
./venv/bin/python -m pip install -q -r requirements.txt
chmod +x run.sh

# --- `tapo-camera-viewer` command
mkdir -p "$HOME/.local/bin"
ln -sf "$DIR/run.sh" "$HOME/.local/bin/$APP"

# --- app menu entry (Linux)
if [ "$(uname)" = "Linux" ]; then
    mkdir -p "$HOME/.local/share/applications"
    cat > "$HOME/.local/share/applications/$APP.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Tapo Camera Viewer
Comment=Live view and controls for Tapo cameras on your network
Exec=$DIR/run.sh
Icon=camera-web
Terminal=false
Categories=AudioVideo;Video;
DESKTOP
fi

echo
say "Installed."
echo "    Start it with:  $APP"
[ "$(uname)" = "Linux" ] && echo "    or open 'Tapo Camera Viewer' from your app menu."
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) echo "    (~/.local/bin isn't on your PATH - use $DIR/run.sh or add it)" ;;
esac
echo
echo "Before first use, in the Tapo app:"
echo "  1. Camera > Settings > Advanced Settings > Camera Account  -> set a username/password"
echo "  2. Me > Tapo Lab > Third-Party Compatibility               -> turn ON"
