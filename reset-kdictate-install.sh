#!/usr/bin/env bash
# reset-kdictate-install.sh
#
# Developer reset: wipe every kdictate, IBus, and KDE-shortcut artifact
# on the local machine so a follow-up `./install.py` exercises the full
# install path from a clean state. The Whisper model.bin is preserved
# (it's multi-gigabyte and stable across resets) and restored after the
# wipe so re-installs don't have to re-download it.
#
# Each step is verified and the script exits non-zero on any failure.
# After this script finishes, run `./install.py`.

set -u

# -- paths ------------------------------------------------------------------

RUNTIME_DIR="$HOME/.local/share/kdictate"
MODEL_DIR="$RUNTIME_DIR/whisper-large-v3-turbo-ct2"
MODEL_BIN="$MODEL_DIR/model.bin"
STAGE_DIR="/tmp/kdictate-model-stage-$$"

STATE_DIR="$HOME/.local/state/kdictate"
IBUS_CACHE="$HOME/.cache/ibus"

IBUS_COMPONENT="$HOME/.local/share/ibus/component/io.github.pizzimenti.KDictate.component.xml"
DBUS_SERVICE="$HOME/.local/share/dbus-1/services/io.github.pizzimenti.KDictate1.service"
SYSTEMD_SERVICE="$HOME/.config/systemd/user/io.github.pizzimenti.KDictate.service"
TOGGLE_DESKTOP="$HOME/.local/share/applications/io.github.pizzimenti.KDictateToggle.desktop"
IBUS_ENV="$HOME/.config/environment.d/60-kdictate-ibus.conf"
PLASMA_ENV="$HOME/.config/plasma-workspace/env/kdictate-plasma-wayland.sh"
KGLOBAL_SHORTCUTS="$HOME/.config/kglobalshortcutsrc"

SERVICE_NAME="io.github.pizzimenti.KDictate.service"
BUS_NAME="io.github.pizzimenti.KDictate1"
ENGINE_NAME="io.github.pizzimenti.KDictate1"

FAILURES=0

say()  { printf '\n==> %s\n' "$*"; }
ok()   { printf '    \u2713 %s\n' "$*"; }
# warn/fail use %b so embedded newlines in $* render as line breaks.
warn() { printf '    ! %b\n' "$*"; }
fail() { printf '    \u2717 %b\n' "$*" >&2; FAILURES=$((FAILURES + 1)); }

check() {
    # check "description" <command...>  -> runs command, errors on non-zero
    local desc=$1; shift
    if "$@"; then
        ok "$desc"
    else
        fail "$desc (command failed: $*)"
    fi
}

# -- 1. Stop systemd user service and clear the unit ----------------------

say "Stopping systemd user service"
systemctl --user stop "$SERVICE_NAME" 2>/dev/null || true
systemctl --user disable "$SERVICE_NAME" 2>/dev/null || true
state=$(systemctl --user is-active "$SERVICE_NAME" 2>/dev/null || true)
case "$state" in
    inactive|failed|"")
        ok "systemd unit is $state"
        ;;
    *)
        fail "systemd unit still $state"
        ;;
esac

# -- 2. Kill every kdictate process ---------------------------------------

say "Killing kdictate processes"
pkill -9 -f kdictate-daemon      2>/dev/null || true
pkill -9 -f ibus-engine-kdictate 2>/dev/null || true
sleep 0.3
if pgrep -f 'kdictate-daemon|ibus-engine-kdictate' >/dev/null; then
    fail "kdictate processes still running:"
    pgrep -af 'kdictate-daemon|ibus-engine-kdictate' | sed 's/^/       /' >&2
else
    ok "no kdictate processes running"
fi

# -- 3. Kill every ibus process (KWin will relaunch later) ----------------

say "Killing ibus processes"
pkill -9 -f ibus-ui-gtk3        2>/dev/null || true
pkill -9 -f "ibus-daemon"       2>/dev/null || true
pkill -9 -f ibus-dconf          2>/dev/null || true
pkill -9 -f ibus-extension      2>/dev/null || true
pkill -9 -f ibus-portal         2>/dev/null || true
pkill -9 -f ibus-x11            2>/dev/null || true
pkill -9 -f ibus-engine-simple  2>/dev/null || true
sleep 0.3
if pgrep -f 'ibus-(ui-gtk3|daemon|dconf|extension|portal|x11|engine-simple)' >/dev/null; then
    warn "ibus processes respawned (KWin's virtual keyboard relaunched them):"
    pgrep -af 'ibus-(ui-gtk3|daemon|dconf|extension|portal|x11|engine-simple)' | sed 's/^/       /'
    printf '      step 13 will cycle KWin VK again after the cache is cleared.\n'
else
    ok "no ibus processes running"
fi

# -- 4. Preserve model.bin ------------------------------------------------

if [[ -f "$MODEL_BIN" ]]; then
    say "Preserving model.bin to $STAGE_DIR"
    mkdir -p "$STAGE_DIR"
    if cp -a "$MODEL_BIN" "$STAGE_DIR/model.bin"; then
        src_size=$(stat -c %s "$MODEL_BIN")
        dst_size=$(stat -c %s "$STAGE_DIR/model.bin")
        if [[ "$src_size" == "$dst_size" ]]; then
            ok "model.bin staged ($src_size bytes)"
        else
            fail "staged model.bin size mismatch ($src_size vs $dst_size)"
        fi
    else
        fail "failed to stage model.bin"
    fi
else
    warn "no model.bin found at $MODEL_BIN -- reinstall will re-download"
fi

# -- 5. Wipe runtime dir --------------------------------------------------

say "Removing runtime dir $RUNTIME_DIR"
rm -rf "$RUNTIME_DIR"
if [[ -e "$RUNTIME_DIR" ]]; then
    fail "$RUNTIME_DIR still exists"
else
    ok "runtime dir removed"
fi

# -- 6. Wipe state/log dir ------------------------------------------------

say "Removing state dir $STATE_DIR"
rm -rf "$STATE_DIR"
if [[ -e "$STATE_DIR" ]]; then
    fail "$STATE_DIR still exists"
else
    ok "state dir removed"
fi

# -- 7. Wipe installed config files outside runtime dir ------------------

say "Removing installed config files"
for f in \
    "$IBUS_COMPONENT" \
    "$DBUS_SERVICE" \
    "$SYSTEMD_SERVICE" \
    "$TOGGLE_DESKTOP" \
    "$IBUS_ENV" \
    "$PLASMA_ENV"
do
    if [[ -e "$f" ]]; then
        rm -f "$f"
        if [[ -e "$f" ]]; then
            fail "could not remove $f"
        else
            ok "removed $f"
        fi
    fi
done

# -- 8. systemctl daemon-reload so the removed unit is forgotten ----------

say "Reloading systemd user manager"
if systemctl --user daemon-reload 2>/dev/null; then
    ok "systemctl --user daemon-reload"
else
    fail "systemctl --user daemon-reload failed"
fi

# -- 9. Reset dconf IBus preload-engines ----------------------------------

if command -v dconf >/dev/null; then
    say "Clearing kdictate from dconf preload-engines"
    current=$(dconf read /desktop/ibus/general/preload-engines 2>/dev/null || true)
    if [[ -n "$current" && "$current" == *"$ENGINE_NAME"* ]]; then
        dconf reset /desktop/ibus/general/preload-engines
    fi
    after=$(dconf read /desktop/ibus/general/preload-engines 2>/dev/null || true)
    if [[ -n "$after" && "$after" == *"$ENGINE_NAME"* ]]; then
        fail "dconf preload-engines still contains $ENGINE_NAME"
    else
        ok "dconf preload-engines clean"
    fi
else
    warn "dconf not installed; skipping"
fi

# -- 10. Strip kdictate entry from kglobalshortcutsrc --------------------

if [[ -f "$KGLOBAL_SHORTCUTS" ]]; then
    say "Stripping kdictate entry from $KGLOBAL_SHORTCUTS"
    section="[services][io.github.pizzimenti.KDictateToggle.desktop]"
    if grep -qF "$section" "$KGLOBAL_SHORTCUTS"; then
        tmp=$(mktemp)
        awk -v section="$section" '
            BEGIN { skip = 0 }
            /^\[/ { skip = ($0 == section) ? 1 : 0 }
            !skip { print }
        ' "$KGLOBAL_SHORTCUTS" > "$tmp" && mv "$tmp" "$KGLOBAL_SHORTCUTS"
        if grep -qF "$section" "$KGLOBAL_SHORTCUTS"; then
            fail "section still present in $KGLOBAL_SHORTCUTS"
        else
            ok "section removed from $KGLOBAL_SHORTCUTS"
        fi
    else
        ok "no kdictate section in $KGLOBAL_SHORTCUTS"
    fi
fi

# -- 11. Wipe the IBus binary cache --------------------------------------

say "Removing IBus cache $IBUS_CACHE"
rm -rf "$IBUS_CACHE"
if [[ -e "$IBUS_CACHE" ]]; then
    fail "$IBUS_CACHE still exists"
else
    ok "IBus cache removed"
fi

# -- 12. Rebuild KDE desktop file index (ksycoca) ------------------------

if command -v kbuildsycoca6 >/dev/null; then
    say "Rebuilding KDE desktop file index"
    if kbuildsycoca6 --noincremental >/dev/null 2>&1; then
        ok "kbuildsycoca6 --noincremental"
    else
        fail "kbuildsycoca6 failed"
    fi
fi

# -- 13. Cycle KWin VirtualKeyboard so ibus-ui-gtk3 respawns -------------

if command -v gdbus >/dev/null; then
    say "Cycling KWin VirtualKeyboard"
    gdbus call --session --dest org.kde.KWin \
        --object-path /VirtualKeyboard \
        --method org.freedesktop.DBus.Properties.Set \
        org.kde.kwin.VirtualKeyboard enabled '<boolean false>' \
        >/dev/null 2>&1 && ok "VK off" || fail "VK off call failed"
    sleep 1
    gdbus call --session --dest org.kde.KWin \
        --object-path /VirtualKeyboard \
        --method org.freedesktop.DBus.Properties.Set \
        org.kde.kwin.VirtualKeyboard enabled '<boolean true>' \
        >/dev/null 2>&1 && ok "VK on" || fail "VK on call failed"
    sleep 2
fi

# -- 14. Verify the daemon bus name is free -------------------------------

if command -v gdbus >/dev/null; then
    say "Verifying $BUS_NAME is free on the session bus"
    if gdbus introspect --session --dest "$BUS_NAME" \
        --object-path /io/github/pizzimenti/KDictate1 \
        >/dev/null 2>&1; then
        fail "$BUS_NAME is still owned"
    else
        ok "$BUS_NAME is free"
    fi
fi

# -- 15. Verify the engine is not in the freshly rebuilt IBus cache ------

if command -v ibus >/dev/null; then
    say "Verifying engine is gone from IBus cache"
    # ibus-daemon may have relaunched from the KWin toggle and rebuilt
    # cache; check that rebuilt cache doesn't know our engine.
    if ibus list-engine 2>/dev/null | grep -qF "$ENGINE_NAME"; then
        fail "IBus still lists $ENGINE_NAME"
    else
        ok "engine is not registered with IBus"
    fi
fi

# -- 16. Restore model.bin into the (now empty) runtime dir --------------

if [[ -f "$STAGE_DIR/model.bin" ]]; then
    say "Restoring model.bin to $MODEL_DIR"
    mkdir -p "$MODEL_DIR"
    if mv "$STAGE_DIR/model.bin" "$MODEL_DIR/model.bin"; then
        dst_size=$(stat -c %s "$MODEL_DIR/model.bin")
        ok "model.bin restored ($dst_size bytes)"
        rmdir "$STAGE_DIR" 2>/dev/null || true
    else
        fail "failed to restore model.bin from stage"
    fi
fi

# -- summary --------------------------------------------------------------

printf '\n'
if [[ "$FAILURES" -gt 0 ]]; then
    printf '==> FAILED: %d check(s) did not pass\n' "$FAILURES" >&2
    exit 1
fi
printf '==> Clean complete. Next step: run ./install.py\n'
