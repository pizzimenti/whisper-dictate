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
cp "$SCRIPT_DIR/whisper-dictate-hotkey.service" ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now whisper-dictate
systemctl --user enable --now whisper-dictate-hotkey

echo ""
echo "Done. The daemon and global hotkey listener are enabled."
echo "  The hotkey listener uses KWin's accessibility keyboard monitor and"
echo "  grabs Ctrl+Space directly on Wayland."
echo "  Terminal control remains available via ~/Code/whisper-dictate/dictatectl.py."
echo "  If you run Orca, stop whisper-dictate-hotkey first because both need"
echo "  the same keyboard-monitor D-Bus name."
echo ""
echo "NOTE: Log out and back in (or reboot) for the input group change to take effect."
echo "      The whisper-dictate service will start automatically on your next login."
