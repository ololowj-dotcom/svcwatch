#!/usr/bin/env bash
set -euo pipefail

REPO="${SVCWATCH_REPO:-https://github.com/ololowj-dotcom/svcwatch}"
REF="${SVCWATCH_REF:-main}"
PREFIX="${SVCWATCH_PREFIX:-/opt/svcwatch}"
BIN_LINK="/usr/local/bin/svcwatch"

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
fail() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "please run as root:  curl -fsSL <url> | sudo bash"
[ "$(uname -s)" = "Linux" ] || fail "svcwatch watches Linux services; this is $(uname -s)"

PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
            PYTHON="$candidate"
            break
        fi
    fi
done
[ -n "$PYTHON" ] || fail "Python 3.9 or newer is required (apt install python3 python3-venv)"

command -v git >/dev/null 2>&1 || fail "git is required (apt install git)"

say "Creating virtualenv in $PREFIX"
mkdir -p "$PREFIX"
if ! "$PYTHON" -m venv "$PREFIX/venv" 2>/dev/null; then
    fail "could not create a virtualenv. On Debian/Ubuntu run:  apt install python3-venv"
fi

say "Installing svcwatch ($REF)"
"$PREFIX/venv/bin/python" -m pip install --quiet --upgrade pip
"$PREFIX/venv/bin/python" -m pip install --quiet --upgrade "git+${REPO}@${REF}"

ln -sf "$PREFIX/venv/bin/svcwatch" "$BIN_LINK"
say "Installed: $("$BIN_LINK" --version)"

if [ "${SVCWATCH_NO_SETUP:-0}" = "1" ]; then
    say "Done. Next step:  sudo svcwatch setup"
    exit 0
fi

if [ -r /dev/tty ] && [ -w /dev/tty ]; then
    say "Starting the guided setup (Ctrl+C to skip; run 'sudo svcwatch setup' later)"
    exec "$BIN_LINK" setup </dev/tty
fi
say "Done. Next step:  sudo svcwatch setup"
