#!/usr/bin/env bash
# Set up Firmware Vault on a Linux machine.
#   ./install.sh            Python venv + dependencies, `fv` linked into ~/.local/bin
#   ./install.sh --service  also run the web page as a systemd user service on port 8200
set -euo pipefail
cd "$(dirname "$0")"
APP="$(pwd)"

command -v python3 >/dev/null || { echo "Python 3.11+ is needed (sudo apt install python3 python3-venv)"; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || { echo "Python 3.11 or newer is needed"; exit 1; }
[ -d .venv ] || python3 -m venv .venv || { echo "Could not make a venv (sudo apt install python3-venv)"; exit 1; }
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

mkdir -p "$HOME/.local/bin"
ln -sf "$APP/bin/fv" "$HOME/.local/bin/fv"
echo "fv is linked in ~/.local/bin (make sure that folder is on your PATH)."

if ! command -v arduino-cli >/dev/null; then
  echo "Optional: arduino-cli is not installed. Arduino IDE sketches need it (sudo apt install arduino-cli)."
fi
id -nG | grep -qw dialout || echo "To flash and use serial ports without sudo: sudo usermod -aG dialout $USER (then log out and in)."

if [ "${1:-}" = "--service" ]; then
  mkdir -p "$HOME/.config/systemd/user"
  sed "s|@FV@|$APP/bin/fv|" packaging/firmware-vault.service > "$HOME/.config/systemd/user/firmware-vault.service"
  systemctl --user daemon-reload
  systemctl --user enable --now firmware-vault
  echo "Web page running on http://localhost:8200 (open port 8200 in your firewall to reach it from the LAN)."
  echo "To keep it running when you are logged out: sudo loginctl enable-linger $USER"
fi

echo
echo "Claude Code (optional): claude mcp add --scope user firmware-vault -- $APP/bin/fv mcp"
echo "Claude Code skill (optional): ln -s $APP/skill ~/.claude/skills/firmware-vault"
echo "Tests: ./test.sh"
