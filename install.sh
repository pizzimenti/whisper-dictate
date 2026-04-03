#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="${SCRIPT_DIR}/$(basename "${BASH_SOURCE[0]}")"
SERVICE_NAME="io.github.pizzimenti.WhisperDictate.service"
DBUS_SERVICE_NAME="io.github.pizzimenti.WhisperDictate1.service"
IBUS_COMPONENT_NAME="io.github.pizzimenti.WhisperDictate.component.xml"
ENGINE_LAUNCHER_NAME="ibus-engine-whisper-dictate"
ENGINE_LAUNCHER_TEMPLATE="${SCRIPT_DIR}/packaging/${ENGINE_LAUNCHER_NAME}"
TOGGLE_DESKTOP_NAME="io.github.pizzimenti.WhisperDictateToggle.desktop"
TOGGLE_DESKTOP_TEMPLATE="${SCRIPT_DIR}/packaging/${TOGGLE_DESKTOP_NAME}"
IBUS_ENV_FILE_NAME="60-whisper-dictate-ibus.conf"
IBUS_ENV_TEMPLATE="${SCRIPT_DIR}/packaging/${IBUS_ENV_FILE_NAME}"
KDE_VIRTUAL_KEYBOARD_DESKTOP="/usr/share/applications/org.freedesktop.IBus.Panel.Wayland.Gtk3.desktop"
PLASMA_ENV_SCRIPT_NAME="whisper-dictate-plasma-wayland.sh"
PLASMA_ENV_SCRIPT_TEMPLATE="${SCRIPT_DIR}/packaging/${PLASMA_ENV_SCRIPT_NAME}"

log() {
    printf '%s\n' "==> $*"
}

die() {
    printf '%s\n' "error: $*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

run_as_user() {
    if [[ -n "${PKEXEC_UID:-}" ]]; then
        sudo -u "#${PKEXEC_UID}" XDG_RUNTIME_DIR="/run/user/${PKEXEC_UID}" HOME="$HOME" "$@"
    else
        "$@"
    fi
}

install_rendered_file() {
    local source_file="$1"
    local destination_file="$2"
    local mode="${3:-0644}"
    local parent_dir

    parent_dir="$(dirname "$destination_file")"
    run_as_user mkdir -p "$parent_dir"
    run_as_user bash -lc "sed -e 's|@@REPO_DIR@@|${REPO_DIR_ESCAPED}|g' -e 's|@@ENGINE_EXEC@@|${ENGINE_EXEC_ESCAPED}|g' -e 's|@@HOME@@|${HOME_ESCAPED}|g' '$source_file' > '$destination_file'"
    run_as_user chmod "$mode" "$destination_file"
}

install_copied_file() {
    local source_file="$1"
    local destination_file="$2"
    local parent_dir

    parent_dir="$(dirname "$destination_file")"
    run_as_user mkdir -p "$parent_dir"
    run_as_user install -m 0644 "$source_file" "$destination_file"
}

if [[ $EUID -ne 0 ]]; then
    exec pkexec bash "$SELF" "$@"
fi

if [[ -n "${PKEXEC_UID:-}" ]]; then
    HOME="$(getent passwd "$PKEXEC_UID" | cut -d: -f6)"
    export HOME
fi

ENGINE_LAUNCHER_PATH="${HOME}/.local/bin/${ENGINE_LAUNCHER_NAME}"
REPO_DIR_ESCAPED="$(printf '%s' "$SCRIPT_DIR" | sed -e 's/[&|\\]/\\&/g')"
ENGINE_EXEC_ESCAPED="$(printf '%s' "$ENGINE_LAUNCHER_PATH" | sed -e 's/[&|\\]/\\&/g')"
HOME_ESCAPED="$(printf '%s' "$HOME" | sed -e 's/[&|\\]/\\&/g')"
IBUS_COMPONENT_PATH_VALUE="${HOME}/.local/share/ibus/component:/usr/share/ibus/component"
IBUS_COMPONENT_PATH_ESCAPED="$(printf '%s' "$IBUS_COMPONENT_PATH_VALUE" | sed -e 's/[&|\\]/\\&/g')"

require_command pacman
require_command python3
require_command systemctl
require_command gdbus
require_command sed

log "Installing required system package: ibus"
pacman -S --noconfirm --needed ibus

require_command ibus
require_command ibus-daemon

log "Creating Python virtual environment"
run_as_user python3 -m venv "$SCRIPT_DIR/.venv"

log "Installing Python dependencies"
run_as_user "$SCRIPT_DIR/.venv/bin/pip" install --upgrade pip
run_as_user "$SCRIPT_DIR/.venv/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"

log "Installing systemd user service"
install_rendered_file \
    "$SCRIPT_DIR/systemd/$SERVICE_NAME" \
    "$HOME/.config/systemd/user/$SERVICE_NAME"

log "Installing D-Bus activation service"
install_rendered_file \
    "$SCRIPT_DIR/packaging/io.github.pizzimenti.WhisperDictate.service" \
    "$HOME/.local/share/dbus-1/services/$DBUS_SERVICE_NAME"

log "Installing IBus component metadata"
install_rendered_file \
    "$SCRIPT_DIR/packaging/$IBUS_COMPONENT_NAME" \
    "$HOME/.local/share/ibus/component/$IBUS_COMPONENT_NAME"

log "Installing IBus component-path environment"
install_rendered_file \
    "$IBUS_ENV_TEMPLATE" \
    "$HOME/.config/environment.d/$IBUS_ENV_FILE_NAME"

log "Installing Plasma Wayland environment cleanup"
install_copied_file \
    "$PLASMA_ENV_SCRIPT_TEMPLATE" \
    "$HOME/.config/plasma-workspace/env/$PLASMA_ENV_SCRIPT_NAME"

log "Installing IBus engine launcher"
install_rendered_file \
    "$ENGINE_LAUNCHER_TEMPLATE" \
    "$ENGINE_LAUNCHER_PATH" \
    0755

log "Installing KDE shortcut launcher"
install_rendered_file \
    "$TOGGLE_DESKTOP_TEMPLATE" \
    "$HOME/.local/share/applications/$TOGGLE_DESKTOP_NAME"
if command -v kbuildsycoca6 >/dev/null 2>&1; then
    run_as_user kbuildsycoca6 --noincremental >/dev/null 2>&1 || true
fi

if command -v kwriteconfig6 >/dev/null 2>&1; then
    log "Configuring KDE Wayland to use IBus Wayland as the virtual keyboard"
    if [[ -f "$KDE_VIRTUAL_KEYBOARD_DESKTOP" ]]; then
        run_as_user kwriteconfig6 \
            --file "$HOME/.config/kwinrc" \
            --group Wayland \
            --key InputMethod \
            "$KDE_VIRTUAL_KEYBOARD_DESKTOP"
    else
        log "Warning: $KDE_VIRTUAL_KEYBOARD_DESKTOP not found; skipping InputMethod configuration"
    fi
    run_as_user kwriteconfig6 \
        --file "$HOME/.config/kwinrc" \
        --group Wayland \
        --key VirtualKeyboardEnabled \
        true
fi

log "Refreshing the IBus engine registry for the current session"
run_as_user bash -lc \
    "IBUS_COMPONENT_PATH='${IBUS_COMPONENT_PATH_ESCAPED}' ibus write-cache && \
     IBUS_COMPONENT_PATH='${IBUS_COMPONENT_PATH_ESCAPED}' ibus-daemon -drx -r -t refresh"

log "Reloading the user systemd manager"
run_as_user systemctl --user daemon-reload
run_as_user systemctl --user enable --now "$SERVICE_NAME"

echo
echo "Done."
echo "  Systemd user service: $SERVICE_NAME"
echo "  D-Bus activation name: io.github.pizzimenti.WhisperDictate1"
echo "  IBus component metadata: $IBUS_COMPONENT_NAME"
echo "  IBus environment file: $HOME/.config/environment.d/$IBUS_ENV_FILE_NAME"
echo "  Plasma env cleanup: $HOME/.config/plasma-workspace/env/$PLASMA_ENV_SCRIPT_NAME"
echo "  IBus engine launcher: $ENGINE_LAUNCHER_PATH"
echo "  KDE shortcut launcher: $HOME/.local/share/applications/$TOGGLE_DESKTOP_NAME"
echo
echo "Select the Whisper Dictate engine from IBus after the frontend is installed."
echo "On KDE Wayland, the installer also selects IBus Wayland as the virtual keyboard when KDE tools are available."
echo "The installer refreshes the IBus cache and restarts ibus-daemon for the current session."
echo "After the first install on KDE Wayland, sign out and back in once so KWin picks up the new input-method configuration."
echo "The core daemon now stays on the transcription side of the boundary only."
