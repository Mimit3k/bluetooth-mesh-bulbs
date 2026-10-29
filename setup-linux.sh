#!/usr/bin/env bash
# Create the local Python environment for Mesh Bulbs on Linux.
#
#   ./setup-linux.sh              # create .venv and install the desktop entry
#   INSTALL_DESKTOP=0 ./setup-linux.sh
#   PYTHON=/usr/bin/python3.13 ./setup-linux.sh
set -euo pipefail

cd "$(dirname "$0")"
ROOT=$(pwd)
PYTHON=${PYTHON:-python3}

if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    echo "Mesh Bulbs needs Python 3.12 or newer (found: $("$PYTHON" --version 2>&1 || echo 'none'))." >&2
    echo "Install python3 (and python3-venv) and run this script again." >&2
    exit 1
fi

# Prefer uv when it is already installed: some distributions ship a Python
# without ensurepip, in which case `python -m venv` produces a venv without pip.
UV=""
if command -v uv >/dev/null 2>&1; then
    UV=$(command -v uv)
elif [ -x "$HOME/.local/bin/uv" ]; then
    UV="$HOME/.local/bin/uv"
fi

echo "Using $("$PYTHON" --version 2>&1)"

if [ ! -x .venv/bin/python ]; then
    echo "Creating .venv ..."
    if [ -n "$UV" ]; then
        "$UV" venv .venv --python "$PYTHON"
    elif ! "$PYTHON" -m venv .venv; then
        echo "Could not create the virtual environment." >&2
        echo "On Debian/Ubuntu install python3-venv (or python3.14-venv) first." >&2
        exit 1
    fi
else
    echo "Reusing existing .venv ..."
fi

echo "Installing dependencies ..."
if .venv/bin/python -m pip --version >/dev/null 2>&1; then
    .venv/bin/python -m pip install --upgrade pip
    .venv/bin/python -m pip install -r requirements.txt
elif [ -n "$UV" ]; then
    "$UV" pip install --python .venv/bin/python -r requirements.txt
else
    echo "The virtual environment has no pip." >&2
    echo "On Debian/Ubuntu install python3-venv (or python3.14-venv), remove .venv, and retry." >&2
    exit 1
fi

.venv/bin/python -c "import bleak, cryptography, PyQt6; print('Dependencies import correctly.')"

if [ "${INSTALL_DESKTOP:-1}" != "0" ]; then
    data_home=${XDG_DATA_HOME:-$HOME/.local/share}
    applications="$data_home/applications"
    mkdir -p "$applications"
    sed "s#@ROOT@#$ROOT#g" packaging/bulbgui.desktop.in > "$applications/bulbgui.desktop"
    echo "Installed the application menu entry: $applications/bulbgui.desktop"
fi

echo
echo "Done. Start the app with:  ./bulbgui"
echo "CLI:                       ./bulbctl devices"
echo
echo "Mesh keys live in ~/.config/bulbctl/state.json. Treat that file like a password."
echo "Bluetooth must be unblocked:  sudo rfkill unblock bluetooth"
