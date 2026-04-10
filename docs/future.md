# KDictate HUD -- Future Work

## Known Limitations

### Wayland popup positioning is best-effort

Phase 1 uses `Gtk.Window(type=POPUP)` with `window.move(x, y)` to place
the HUD at the bottom-right of the primary monitor workarea.  This works
on KDE Plasma Wayland (KWin honours move hints for popup windows) but is
not guaranteed by the Wayland protocol.  Other compositors may ignore the
position or place the window elsewhere.

The current placement is intentionally simple: primary monitor,
screen-corner fallback, no caret-following.  It is not a stable
positioning API.

## Milestones

### M0 -- Resident lifecycle and singleton behavior

Keep the HUD resident without letting it become noisy or fragile across
logins, installs, and daemon restarts.

- Make the HUD a singleton per session
- Ensure repeated autostart / manual launch requests do not create duplicates
- Decide whether long-term startup belongs in KDE autostart, a user service,
  or a small D-Bus-activated wrapper
- Optionally launch the HUD immediately after install so "resident in memory"
  does not wait for the next login
- Keep HUD failure isolated so it never affects daemon or IBus operation

### M1 -- Layer-shell positioning (replace move heuristic)

Move the HUD to `gtk-layer-shell` (or the equivalent KDE Plasma protocol)
so positioning is compositor-native rather than heuristic.

- Use `zwlr_layer_shell_v1` via `gtk-layer-shell` Python bindings
- Anchor to bottom-right of the output
- Set exclusive zone to 0 (overlay, no reserved space)
- Remove the `window.move()` fallback
- Keeps GTK3 as the toolkit

### M2 -- Qt/KDE-first rewrite

Replace the GTK3 HUD with a Qt6/QML implementation that integrates
natively with Plasma.

- Use KDE Frameworks (KWayland, KWindowSystem) for layer-shell
- Use Kirigami or plain QML for the overlay surface
- Use Qt D-Bus (QDBusConnection) instead of Gio for signal subscription
- Integrate with Plasma's OSD / notification styling
- Drop the GTK3 dependency from the HUD process entirely
- Preserve the current daemon / IBus / HUD separation of concerns

Prerequisites:
- PySide6 or PyQt6 available as a dependency (or rewrite in C++)
- Decide whether the HUD remains a separate process or merges with
  the IBus engine (both run under Qt if the engine moves to Qt too)

### M3 -- Caret-following (Phase 2-3 of ibus-ui-plan.md)

Teach the HUD to follow the text cursor when anchor data is available.

- IBus engine publishes minimal focus/caret metadata on a side channel
- HUD consumes the caret rectangle and positions near the insertion point
- Throttle anchor-driven movement to avoid jitter
- Fall back to panel-side corner mode when anchor data is stale or missing
- Preserve Phase 1 panel-side mode as the reliable baseline

### M4 -- Multi-monitor awareness

Allow the HUD to appear on the monitor where the user is actively typing.

- Consume focus/caret metadata from M3 to determine the active output
- Or fall back to the monitor containing the focused window
- Avoid chasing the pointer -- use input focus, not cursor position

### M5 -- Hardening and observability

Turn the HUD into a boring resident component that is easy to debug.

- Add a dedicated HUD instance guard with clear logs on duplicate launch
- Add explicit shutdown / cleanup on session exit
- Improve structured logging around bridge reconnects and seed invalidation
- Expose a simple "am I running?" diagnostic for troubleshooting
- Document expected behavior when the daemon is absent but the HUD is resident

### M6 -- UI and integration testing

Add small but real coverage around the shell, not just the reducer.

- Add a GTK smoke test for show / hide / update_presentation
- Add bridge tests for restart storms and malformed replies
- Add installer / packaging coverage for the HUD desktop file and KDE scoping
- Keep screenshot and compositor-specific tests out of scope until placement
  is more stable

## Out of scope

- Turning the HUD into a second text insertion or editing mechanism
- Pixel-perfect screenshot tests
- Non-KDE desktop integration (GNOME, Sway) beyond basic GTK fallback
