#!/usr/bin/env sh
# HireLoop installer for macOS and Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/RahulSankhla312/hireloop/main/install.sh | sh
#
# Installs HireLoop with pipx (its own isolated Python environment), sets up the
# browser it fills forms with, and opens it. Your profile, jobs and settings are
# kept in ~/.hireloop.
set -eu

SOURCE="git+https://github.com/RahulSankhla312/hireloop.git"
say() { printf '\033[36m==> %s\033[0m\n' "$1"; }

PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PYTHON="$candidate"; break
  fi
done
if [ -z "$PYTHON" ]; then
  echo "HireLoop needs Python 3.11 or newer."
  echo "  macOS:  brew install python@3.12"
  echo "  Ubuntu: sudo apt install python3.12 python3.12-venv"
  echo "Then run this installer again."
  exit 1
fi
say "Using $($PYTHON --version)"

if ! command -v pipx >/dev/null 2>&1; then
  say "Installing pipx"
  "$PYTHON" -m pip install --user --upgrade --quiet pipx 2>/dev/null ||
    "$PYTHON" -m pip install --user --upgrade --quiet --break-system-packages pipx
  "$PYTHON" -m pipx ensurepath >/dev/null
  PIPX="$PYTHON -m pipx"
else
  PIPX="pipx"
fi

say "Installing HireLoop (a few minutes the first time)"
$PIPX install --force --python "$PYTHON" "$SOURCE"

say "Setting up the form-filling browser"
VENVS="$($PIPX environment --value PIPX_LOCAL_VENVS)"
"$VENVS/hireloop/bin/python" -m playwright install --with-deps chromium 2>/dev/null ||
  "$VENVS/hireloop/bin/python" -m playwright install chromium ||
  echo "Skipped - HireLoop will use your installed Chrome instead."

BIN="$($PIPX environment --value PIPX_BIN_DIR)"
echo
printf '\033[32mHireLoop is installed.\033[0m\n'
echo "  Start it any time:  hireloop web   (open a new terminal first if the command isn't found)"
echo "  Just looking?       hireloop demo"
echo
say "Opening HireLoop - keep this terminal open while you use it (Ctrl+C to stop)"
exec "$BIN/hireloop" web
