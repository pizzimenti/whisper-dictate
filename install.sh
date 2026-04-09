#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="${SCRIPT_DIR}/$(basename "${BASH_SOURCE[0]}")"
SERVICE_NAME="io.github.pizzimenti.KDictate.service"
DBUS_SERVICE_NAME="io.github.pizzimenti.KDictate1.service"
IBUS_COMPONENT_NAME="io.github.pizzimenti.KDictate.component.xml"
ENGINE_LAUNCHER_NAME="ibus-engine-kdictate"
ENGINE_LAUNCHER_TEMPLATE="${SCRIPT_DIR}/packaging/${ENGINE_LAUNCHER_NAME}.sh"
TOGGLE_DESKTOP_NAME="io.github.pizzimenti.KDictateToggle.desktop"
TOGGLE_DESKTOP_TEMPLATE="${SCRIPT_DIR}/packaging/${TOGGLE_DESKTOP_NAME}"
IBUS_ENV_FILE_NAME="60-kdictate-ibus.conf"
IBUS_ENV_TEMPLATE="${SCRIPT_DIR}/packaging/${IBUS_ENV_FILE_NAME}"
KDE_VIRTUAL_KEYBOARD_DESKTOP="/usr/share/applications/org.freedesktop.IBus.Panel.Wayland.Gtk3.desktop"
PLASMA_ENV_SCRIPT_NAME="kdictate-plasma-wayland.sh"
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
    # Pass paths and substitution values as positional parameters so a path
    # containing a single quote (e.g., a home directory like /home/o'brien)
    # cannot break out of the shell quoting and inject commands into the
    # elevated subprocess.
    run_as_user bash -lc \
        'sed -e "s|@@REPO_DIR@@|${1}|g" -e "s|@@ENGINE_EXEC@@|${2}|g" -e "s|@@HOME@@|${3}|g" "${4}" > "${5}"' \
        -- "$REPO_DIR_ESCAPED" "$ENGINE_EXEC_ESCAPED" "$HOME_ESCAPED" "$source_file" "$destination_file"
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

# Sync the runtime files (package + pyproject + requirements) from the
# source tree into $RUNTIME_DIR. Idempotent. Does NOT touch the venv or the
# models/ directory — those are handled separately so updates don't redownload
# the 3+ GB model files.
sync_runtime() {
    run_as_user mkdir -p "$RUNTIME_DIR"
    log "Syncing source files to $RUNTIME_DIR"
    run_as_user rsync -a --delete \
        "$SCRIPT_DIR/kdictate/" "$RUNTIME_DIR/kdictate/"
    run_as_user install -Dm644 "$SCRIPT_DIR/requirements.txt" "$RUNTIME_DIR/requirements.txt"
    run_as_user install -Dm644 "$SCRIPT_DIR/pyproject.toml"   "$RUNTIME_DIR/pyproject.toml"
}

# --- argument parsing ---
SYNC_ONLY=0
if [[ "${1:-}" == "--sync-only" ]]; then
    SYNC_ONLY=1
fi

# --- fast path: --sync-only just rsyncs source -> runtime and restarts the daemon.
# Used for the dev edit loop. No root, no pacman, no venv recreation, models untouched.
if [[ "$SYNC_ONLY" == "1" ]]; then
    if [[ $EUID -eq 0 ]]; then
        die "--sync-only must run as your user, not root"
    fi
    RUNTIME_DIR="$HOME/.local/share/kdictate"
    sync_runtime
    # Capture stderr so a real failure (unit syntax error, missing
    # dependency, etc.) is visible to the user instead of being
    # silently swallowed alongside the legitimate "service not running"
    # case.
    if ! restart_output="$(systemctl --user restart "$SERVICE_NAME" 2>&1)"; then
        log "Service restart skipped or failed (source synced): ${restart_output:-no detail}"
    fi
    log "Sync-only complete. RUNTIME_DIR=$RUNTIME_DIR"
    exit 0
fi

if [[ $EUID -ne 0 ]]; then
    exec pkexec bash "$SELF" "$@"
fi

if [[ -n "${PKEXEC_UID:-}" ]]; then
    HOME="$(getent passwd "$PKEXEC_UID" | cut -d: -f6)"
    export HOME
fi

RUNTIME_DIR="${HOME}/.local/share/kdictate"
ENGINE_LAUNCHER_PATH="${HOME}/.local/bin/${ENGINE_LAUNCHER_NAME}"
REPO_DIR_ESCAPED="$(printf '%s' "$RUNTIME_DIR" | sed -e 's/[&|\\]/\\&/g')"
ENGINE_EXEC_ESCAPED="$(printf '%s' "$ENGINE_LAUNCHER_PATH" | sed -e 's/[&|\\]/\\&/g')"
HOME_ESCAPED="$(printf '%s' "$HOME" | sed -e 's/[&|\\]/\\&/g')"
IBUS_COMPONENT_PATH_VALUE="${HOME}/.local/share/ibus/component:/usr/share/ibus/component"

require_command pacman
require_command python3
require_command systemctl
require_command gdbus
require_command sed
require_command rsync

log "Installing required system package: ibus"
pacman -S --noconfirm --needed ibus

require_command ibus
require_command ibus-daemon

sync_runtime

# One-time migration of the model directory out of the source tree.
# Skipped on subsequent runs so updates do not redownload the 3+ GB of model data.
if [[ -d "$SCRIPT_DIR/models" && ! -e "$RUNTIME_DIR/models" ]]; then
    if [[ -L "$SCRIPT_DIR/models" ]]; then
        # If the user symlinked models/ (e.g., to external storage), don't
        # `mv` the symlink — that would relocate a possibly-relative link
        # and silently break the reference. Resolve to an absolute target,
        # recreate the symlink at the runtime location, and remove the
        # original. The 3+ GB of model data stays where the user put it.
        models_target="$(readlink -f "$SCRIPT_DIR/models")"
        log "models/ is a symlink → $models_target; recreating link at $RUNTIME_DIR/models"
        run_as_user ln -s "$models_target" "$RUNTIME_DIR/models"
        run_as_user rm "$SCRIPT_DIR/models"
    else
        log "Migrating models/ from source tree to $RUNTIME_DIR/models (one-time)"
        run_as_user mv "$SCRIPT_DIR/models" "$RUNTIME_DIR/models"
    fi
fi

# The venv is recreated unconditionally on every full install. This is
# intentional: full installs require pkexec and are infrequent, so a
# clean venv guarantees consistency across upgrades. The dev edit loop
# uses --sync-only above which skips this entirely. If venv recreation
# becomes a pain point, gate this behind `[[ ! -d "$RUNTIME_DIR/.venv" ]]`
# or add a `--recreate-venv` flag.
log "Creating Python virtual environment in $RUNTIME_DIR/.venv"
run_as_user python3 -m venv "$RUNTIME_DIR/.venv"

log "Installing Python dependencies"
run_as_user "$RUNTIME_DIR/.venv/bin/pip" install --upgrade pip
run_as_user "$RUNTIME_DIR/.venv/bin/pip" install -r "$RUNTIME_DIR/requirements.txt"

log "Installing systemd user service"
install_rendered_file \
    "$SCRIPT_DIR/systemd/$SERVICE_NAME" \
    "$HOME/.config/systemd/user/$SERVICE_NAME"

log "Installing D-Bus activation service"
install_rendered_file \
    "$SCRIPT_DIR/packaging/io.github.pizzimenti.KDictate.service" \
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
# Pass IBUS_COMPONENT_PATH as a positional parameter so a $HOME containing
# a single quote (e.g., /home/o'brien) cannot break out of shell quoting
# and inject commands into the elevated subprocess. Same fix class as
# install_rendered_file. The unescaped IBUS_COMPONENT_PATH_VALUE is fine
# here because we're not running it through sed — only sed needs the
# &|\ escaping.
run_as_user bash -lc \
    'IBUS_COMPONENT_PATH="$1" ibus write-cache && IBUS_COMPONENT_PATH="$1" ibus-daemon -drx -r -t refresh' \
    -- "$IBUS_COMPONENT_PATH_VALUE"

log "Reloading the user systemd manager"
run_as_user systemctl --user daemon-reload
run_as_user systemctl --user enable "$SERVICE_NAME"
run_as_user systemctl --user restart "$SERVICE_NAME"

echo
echo "Done."
echo "  Systemd user service: $SERVICE_NAME"
echo "  D-Bus activation name: io.github.pizzimenti.KDictate1"
echo "  IBus component metadata: $IBUS_COMPONENT_NAME"
echo "  IBus environment file: $HOME/.config/environment.d/$IBUS_ENV_FILE_NAME"
echo "  Plasma env cleanup: $HOME/.config/plasma-workspace/env/$PLASMA_ENV_SCRIPT_NAME"
echo "  IBus engine launcher: $ENGINE_LAUNCHER_PATH"
echo "  KDE shortcut launcher: $HOME/.local/share/applications/$TOGGLE_DESKTOP_NAME"
echo
echo "Select the KDictate engine from IBus after the frontend is installed."
echo "On KDE Wayland, the installer also selects IBus Wayland as the virtual keyboard when KDE tools are available."
echo "The installer refreshes the IBus cache and restarts ibus-daemon for the current session."
echo "After the first install on KDE Wayland, sign out and back in once so KWin picks up the new input-method configuration."
echo "The core daemon now stays on the transcription side of the boundary only."
