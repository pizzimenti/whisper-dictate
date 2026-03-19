#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Installing ydotool"
sudo pacman -S --noconfirm --needed ydotool

echo "==> Enabling ydotool user service"
systemctl --user enable --now ydotool

echo "==> Adding $USER to input group (required for ydotool)"
sudo usermod -aG input "$USER"

echo "==> Creating Python virtual environment"
python3 -m venv "$SCRIPT_DIR/.venv"

echo "==> Installing Python dependencies"
"$SCRIPT_DIR/.venv/bin/pip" install --upgrade pip
"$SCRIPT_DIR/.venv/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"

echo "==> Installing whisper-dictate systemd user service"
mkdir -p ~/.config/systemd/user
sed "s|@@REPO_DIR@@|${SCRIPT_DIR}|g" \
    "$SCRIPT_DIR/whisper-dictate.service" \
    > ~/.config/systemd/user/whisper-dictate.service
systemctl --user daemon-reload
systemctl --user enable --now whisper-dictate

echo ""
echo "Done. The daemon is enabled (hotkey listener is built-in)."
echo "  Ctrl+Space is grabbed via KWin's accessibility keyboard monitor on Wayland."
echo "  Terminal control remains available via ${SCRIPT_DIR}/dictatectl.py."
echo "  If you run Orca, stop whisper-dictate first because both need"
echo "  the same keyboard-monitor D-Bus name."
echo ""
echo "NOTE: Log out and back in (or reboot) for the input group change to take effect."
echo "      The whisper-dictate service will start automatically on your next login."
