#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Installing ydotool"
sudo pacman -S --noconfirm --needed ydotool

echo "==> Enabling ydotool system service"
sudo systemctl enable --now ydotool

echo "==> Adding $USER to input group (required for ydotool)"
sudo usermod -aG input "$USER"

echo "==> Installing whisper-dictate systemd user service"
mkdir -p ~/.config/systemd/user
cp "$SCRIPT_DIR/whisper-dictate.service" ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now whisper-dictate

echo ""
echo "Done. One manual step remaining:"
echo "  Bind ~/Code/whisper-dictate/toggle.sh to a hotkey in:"
echo "  System Settings → Shortcuts → Custom Shortcuts"
echo ""
echo "NOTE: Log out and back in for the input group change to take effect,"
echo "      then restart the service: systemctl --user restart whisper-dictate"
