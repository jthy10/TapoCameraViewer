#!/bin/bash
# resolve symlinks so this works when launched as ~/.local/bin/tapo-camera-viewer
cd "$(dirname "$(readlink -f "$0" 2>/dev/null || python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$0")")"
exec ./venv/bin/python tapo_camera_viewer.py "$@"
