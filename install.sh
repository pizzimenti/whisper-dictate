#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Installing ydotool"
sudo pacman -S --noconfirm --needed ydotool

echo "==> Enabling ydotool user service"
systemctl --user enable --now ydotool

echo "==> Adding $USER to input group (required for ydotool)"
sudo usermod -aG input "$USER"

echo "==> Installing whisper-dictate systemd user service"
mkdir -p ~/.config/systemd/user
cp "$SCRIPT_DIR/whisper-dictate.service" ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now whisper-dictate

echo ""
echo "Done. One manual step remaining:"
echo "  Bind ~/Code/whisper-dictate/ptt-press.sh on key press and"
echo "  ~/Code/whisper-dictate/ptt-release.sh on key release in:"
echo "  System Settings → Shortcuts → Custom Shortcuts"
echo "  ~/Code/whisper-dictate/toggle.sh remains available as a fallback."
echo ""
echo "NOTE: Log out and back in (or reboot) for the input group change to take effect."
echo "      The whisper-dictate service will start automatically on your next login."
