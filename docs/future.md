# KDictate Qt GUI -- Future Work

The early GTK popup experiment is retired. The next UI direction is a
Qt/KDE-first companion that presents dictation state cleanly without becoming a
second text insertion path.

## Working Direction

- Keep the daemon headless and authoritative for dictation state.
- Keep the IBus engine as the only text insertion path.
- Treat the GUI as presentation only.
- Move from the current TUI-oriented workflow toward a Qt GUI companion.
- Prefer Plasma-native behavior and styling over generic cross-desktop UI.

## Visual Direction

- Reference image: `docs/macos-big-sur-siri.webp`
- Use it as a style reference only, not as a behavioral spec.
- The target feel is compact, polished, ephemeral, and voice-assistant-like.
- The eventual Qt overlay should feel closer to an OSD / assistant bubble than
  to a utility window or tray popup.

## Milestones

### M1 -- Qt/KDE-first GUI companion

Build a separate Qt6/QML companion that subscribes to daemon state and renders
ephemeral dictation feedback.

- Use Qt6/QML, with Kirigami only if it helps rather than driving the design
- Use `QDBusConnection` (Qt D-Bus) for daemon subscription, not
  `gi.repository.Gio`
- Consider KDE Frameworks for layer-shell positioning
  (`KWayland`, `KWindowSystem`) rather than re-implementing compositor
  placement heuristics in application code
- Keep the GUI separate from the recognizer and from IBus commit logic
- Start with simple anchored or screen-corner presentation before advanced
  positioning
- Keep failure of the GUI isolated from daemon and IBus behavior

Prerequisites to settle before starting M1:

- Pick the language/toolkit binding: PySide6, PyQt6, or C++ with Qt6. Python
  is the default to match the rest of the codebase; C++ is only worth
  considering if the Qt binding overhead becomes a real problem.
- Decide whether the GUI runs as a separate process subscribing to the daemon
  over D-Bus (current architecture for the retired GTK experiment) or
  co-hosts with the IBus engine. Co-hosting would deduplicate the D-Bus
  client and shorten the state-update path, at the cost of failure
  isolation.
- Add a daemon-side ``GetSnapshot`` D-Bus method that returns an atomic
  ``(state, active_partial, last_final, error_code, error_message)`` tuple
  so a freshly-launched or reconnecting GUI can seed its state without
  replaying signal history. This was prototyped on the retired
  ``ibus-ui-hud`` branch and is cheap to re-land when a real Qt consumer
  exists.

### M2 -- Caret-following and placement

Position the GUI near the user’s insertion point when reliable anchor data is
available.

- Publish minimal anchor metadata from the IBus side only if needed
- Follow the caret when geometry is fresh and trustworthy
- Fall back cleanly when geometry is missing, stale, or inconsistent
- Avoid making placement logic part of transcription or commit behavior

### M3 -- Multi-monitor and focus awareness

Show the GUI on the output that actually matters to the user.

- Prefer focused-input or caret-derived output selection
- Avoid pointer-chasing heuristics
- Keep fallback behavior deterministic when anchor data is unavailable

### M4 -- Lifecycle and observability

Make the GUI a boring resident component that is easy to debug.

- Ensure singleton behavior per session
- Keep logs for startup, daemon connect/disconnect, and placement decisions
- Make it obvious whether the GUI is running and subscribed
- Keep restart behavior predictable across login, daemon restart, and IBus
  restart

### M5 -- UI and integration testing

Add enough coverage to keep the GUI stable without overfitting to compositor
details.

- Unit-test state and presentation mapping
- Add a small Qt smoke test for show/hide/update lifecycle
- Test daemon reconnect behavior and stale-state recovery
- Keep pixel-perfect screenshot tests out of scope unless the design hardens

## Out of Scope

- Any second insertion or editing mechanism
- Mixing GUI behavior into the recognizer or core pipeline
- Reintroducing the retired GTK popup path
- Non-KDE-first UI design as the main target
- Cross-desktop GUI integration (GNOME, Sway, Hyprland, etc.) beyond
  whatever happens to work by virtue of standard Wayland protocols —
  KDictate is deliberately KDE-first, and chasing other desktops is a
  support burden with no corresponding value to the primary user base
