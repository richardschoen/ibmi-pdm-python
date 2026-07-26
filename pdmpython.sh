#!/QOpenSys/pkgs/bin/bash
# Active our virtual environment
source /pythonenv313/bin/activate
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"
exec python pdmpython.py
