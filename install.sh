#!/usr/bin/env bash
set -euo pipefail

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_SERVICE_NAME="whisper-dictate.service"

if [[ $EUID -ne 0 ]]; then
    exec pkexec bash "$SELF" "$@"
fi

run_as_user() {
    if [[ -n "${PKEXEC_UID:-}" ]]; then
        sudo -u "#${PKEXEC_UID}" XDG_RUNTIME_DIR="/run/user/${PKEXEC_UID}" HOME="$HOME" "$@"
    else
        "$@"
    fi
}

if [[ -n "${PKEXEC_UID:-}" ]]; then
    HOME="$(getent passwd "$PKEXEC_UID" | cut -d: -f6)"
    export HOME
fi

echo "==> Installing wl-clipboard"
pacman -S --noconfirm --needed wl-clipboard

echo "==> Creating Python virtual environment"
run_as_user python3 -m venv "$SCRIPT_DIR/.venv"

echo "==> Installing Python dependencies"
run_as_user "$SCRIPT_DIR/.venv/bin/pip" install --upgrade pip
run_as_user "$SCRIPT_DIR/.venv/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"

echo "==> Installing whisper-dictate systemd user service"
run_as_user mkdir -p "$HOME/.config/systemd/user"
run_as_user bash -lc "sed 's|@@REPO_DIR@@|${SCRIPT_DIR}|g' '${SCRIPT_DIR}/whisper-dictate.service' > '${HOME}/.config/systemd/user/${USER_SERVICE_NAME}'"
run_as_user systemctl --user daemon-reload
run_as_user systemctl --user enable --now whisper-dictate

echo ""
echo "Done. The daemon is enabled (hotkey listener is built-in)."
echo "  Ctrl+Space is grabbed via KWin's accessibility keyboard monitor on Wayland."
echo "  Terminal control remains available via ${SCRIPT_DIR}/dictatectl.py."
echo "  If you run Orca, stop whisper-dictate first because both need"
echo "  the same keyboard-monitor D-Bus name."
echo ""
echo "The whisper-dictate service will start automatically on your next login."
