# KDictate D-Bus and Process Orchestration

## Process Topology

```
KWin (compositor)
 └─ ibus-ui-gtk3 --enable-wayland-im     ← KWin starts this as virtual keyboard
     └─ ibus-daemon --xim --panel disable ← ibus-ui-gtk3 starts this
         └─ ibus-engine-kdictate          ← ibus-daemon starts this on demand

systemd --user
 └─ kdictate-daemon --profile service     ← user service, owns mic/VAD/transcription
```

## Who Starts What

| Process | Started by | Lifecycle |
|---------|-----------|-----------|
| `ibus-ui-gtk3` | KWin (when VirtualKeyboard.enabled=true) | Lives for the desktop session |
| `ibus-daemon` | `ibus-ui-gtk3` (--exec-daemon flag) | Lives as long as ibus-ui-gtk3 |
| `ibus-engine-kdictate` | `ibus-daemon` (when engine is activated) | Lives as long as ibus-daemon |
| `kdictate-daemon` | systemd user service | Independent of IBus |

## D-Bus Bus Names

| Bus name | Owner | Purpose |
|----------|-------|---------|
| `io.github.pizzimenti.KDictate1` | kdictate-daemon | Dictation control + transcript signals |
| `org.freedesktop.IBus` | ibus-daemon | IBus framework bus |

## Signal Flow

```
kdictate-daemon (session bus signals)
    StateChanged(state)        ──→  ibus-engine-kdictate (via Gio.bus_watch_name)
    PartialTranscript(text)    ──→  ibus-engine-kdictate
    FinalTranscript(text)      ──→  ibus-engine-kdictate
    ErrorOccurred(code, msg)   ──→  ibus-engine-kdictate

ibus-engine-kdictate (IBus API calls)
    update_preedit_text()      ──→  ibus-daemon ──→ ibus-ui-gtk3 ──→ KWin ──→ focused app
    commit_text()              ──→  ibus-daemon ──→ ibus-ui-gtk3 ──→ KWin ──→ focused app
```

## Critical: Wayland Text Input Chain

On Plasma Wayland, text insertion goes through this chain:

```
IBus engine  →  ibus-daemon  →  ibus-ui-gtk3  →  KWin (text-input protocol)  →  app
```

If ANY link in this chain is stale, restarted out of order, or holding
a dead connection, preedit/commit silently fails. The engine will log
"Committing final transcript" but nothing appears in the app.

## Full Reset Procedure

When preedit/commit stops working, reset the entire chain from the
compositor down:

```bash
# 1. Stop the daemon (prevents signals during reset)
systemctl --user stop io.github.pizzimenti.KDictate.service

# 2. Kill everything IBus-related
pkill -f ibus-engine-kdictate
pkill -f ibus-daemon
pkill -f ibus-ui-gtk3

# 3. Rebuild IBus cache
IBUS_COMPONENT_PATH="$HOME/.local/share/ibus/component:/usr/share/ibus/component" \
    ibus write-cache

# 4. Restart from KWin down (this relaunches ibus-ui-gtk3 → ibus-daemon → engine)
gdbus call --session --dest org.kde.KWin --object-path /VirtualKeyboard \
    --method org.freedesktop.DBus.Properties.Set \
    org.kde.kwin.VirtualKeyboard enabled '<boolean false>'
sleep 1
gdbus call --session --dest org.kde.KWin --object-path /VirtualKeyboard \
    --method org.freedesktop.DBus.Properties.Set \
    org.kde.kwin.VirtualKeyboard enabled '<boolean true>'

# 5. Wait for IBus to settle, then start daemon
sleep 2
systemctl --user start io.github.pizzimenti.KDictate.service

# 6. Activate our engine (may fail silently if already active -- that's OK)
sleep 1
ibus engine io.github.pizzimenti.KDictate1 2>/dev/null || true
```

## Common Failure Modes

### Preedit/commit logged but not visible in app
The Wayland text-input chain is broken. Full reset needed.

### Engine process running but not receiving daemon signals
`ibus-engine-kdictate` couldn't connect to `io.github.pizzimenti.KDictate1`.
Check `~/.local/state/kdictate/ibus-engine.log` for "Daemon bus name appeared"
or "vanished" messages. Restart the daemon service.

### Multiple engine processes
KWin toggle can leave orphan engine processes. Kill all, then toggle.

### `ibus engine <name>` fails with exit code 1
Common on Plasma Wayland even when the engine IS active. Check with
`ibus engine` (no argument) which just prints the current engine name.
