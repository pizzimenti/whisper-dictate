"""Wayland-friendly global hotkey grab via KWin's accessibility KeyboardMonitor.

KDE6 Wayland exposes a single global-shortcut surface usable from arbitrary
processes: ``org.freedesktop.a11y.KeyboardMonitor`` (owned by
``kwin_wayland``). It was designed for screen readers, so KWin gates it
behind a hardcoded D-Bus name allowlist — only a peer that owns
``org.gnome.Orca.KeyboardMonitor`` is allowed to call ``SetKeyGrabs``. We
squat on that name so KWin will accept our grab.

This is the only path that works without a logout/login cycle on a fresh
install:

* ``kglobalshortcutsrc`` is read by kwin only at session start, so writing
  the ini entry at install time has no immediate effect.
* ``org.kde.KGlobalAccel.doRegister`` + ``setShortcut`` will create a
  component, but it stays ``isActive() == false`` and kwin does not route
  keys to inactive components.

The trade-off: while kdictate is running, GNOME Orca cannot grab keys
through the same interface. That is acceptable on a KDE desktop where
Orca is not the screen reader of choice.

GTK/GNOME wart: this whole module talks to a GNOME-named service via
gi.repository because there is no Qt/KDE-native equivalent for grabbing
arbitrary global keys on Wayland. If a KWin-native API ever lands,
replace this module wholesale.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Iterable

from kdictate.logging_utils import configure_logging

# Defaults match the historical kglobal_hotkey.py listener: Ctrl+Space.
DEFAULT_HOTKEY_KEYSYM = 0x0020  # XK_space
DEFAULT_REQUIRED_MODIFIER_MASK = 0x04  # Control
DEFAULT_IGNORED_MODIFIER_MASK = 0x12  # CapsLock + NumLock

# KWin's accessibility KeyboardMonitor delivers two kinds of "extra"
# press events that we have to suppress to avoid firing the activation
# callback more than once per physical keystroke:
#
# 1. Per-modifier-mask fan-out: kwin emits one KeyEvent per registered
#    grab mask permutation. For Ctrl+Space with CapsLock/NumLock
#    permutations, that's 4 events delivered within a few microseconds
#    of the same physical press.
#
# 2. Hardware autorepeat: empirically (Manjaro KDE 6.5.6, 2026-04-11)
#    kwin DOES forward keyboard autorepeat through this interface, at
#    ~25 Hz / 40 ms intervals — confirmed by capturing precise journal
#    timestamps during a held Ctrl+Space and seeing ~60 events over a
#    2-second hold. Our earlier assumption that assistive-tech
#    interfaces suppress autorepeat was wrong.
#
# Both are handled by a *trailing* dedupe: every press event resets
# the window, even one that gets ignored. As long as new events keep
# arriving inside the window, the gate stays closed. The chain only
# "exits" when the user actually stops generating events — i.e. when
# they release the key (whether or not kwin delivers a release
# KeyEvent for that release; see _on_key_event for why we can't rely
# on release events).
#
# Window must be > worst-case autorepeat interval (~60 ms with jitter)
# and < realistic intentional double-tap timing (~200 ms+). 150 ms
# gives ~2.5x headroom over autorepeat and ~30% margin under double-tap.
_PRESS_DEDUPE_WINDOW_S = 0.150

CLIENT_NAME = "org.gnome.Orca.KeyboardMonitor"
MONITOR_BUS_NAME = "org.freedesktop.a11y.Manager"
MONITOR_OBJECT_PATH = "/org/freedesktop/a11y/Manager"
MONITOR_INTERFACE = "org.freedesktop.a11y.KeyboardMonitor"

# D-Bus RequestName request flags (from dbus spec §7.2.1):
#   ALLOW_REPLACEMENT = 0x1 — another peer can steal our name if they
#                             later RequestName it with REPLACE_EXISTING.
#   REPLACE_EXISTING  = 0x2 — if the name is currently owned AND its
#                             owner set ALLOW_REPLACEMENT, take it from
#                             them. Otherwise fail with NAME_EXISTS.
#   DO_NOT_QUEUE      = 0x4 — if the name is currently owned, fail
#                             immediately with NAME_EXISTS instead of
#                             queuing us behind the current owner.
#
# We pass DO_NOT_QUEUE so a failed RequestName is guaranteed to leave
# us with zero bus-name state. Without this flag, flags=0 would let
# dbus queue us behind Orca; _request_client_name would then raise
# (because reply != PRIMARY_OWNER/ALREADY_OWNER), _owns_name would
# stay False, and stop() would not call ReleaseName — so our queued
# claim would survive the failed startup, and when Orca later exits
# we would silently become the screen-reader name owner for the rest
# of the daemon lifetime. Codex flagged this on PR #6.
_DBUS_NAME_FLAG_DO_NOT_QUEUE = 0x4

# D-Bus RequestName reply codes (from dbus spec §7.2.1).
_DBUS_REQUEST_NAME_PRIMARY_OWNER = 1
# IN_QUEUE = 2 is intentionally absent: DO_NOT_QUEUE above guarantees
# we never get queued, so IN_QUEUE cannot be returned.
# EXISTS   = 3 is the failure path — name is owned and we refused to queue.
_DBUS_REQUEST_NAME_ALREADY_OWNER = 4


def expand_modifier_masks(required_mask: int, ignored_mask: int) -> list[int]:
    """Return every modifier mask KWin should treat as equivalent.

    KWin matches grabs by exact mask, so we have to enumerate every
    combination of the ignored bits (Caps/Num lock) on top of the
    required bits. The result is a sorted list with no duplicates.
    """

    masks = [required_mask]
    bit = 1
    remaining = ignored_mask
    while remaining:
        if remaining & bit:
            masks.extend(existing | bit for existing in list(masks))
            remaining &= ~bit
        bit <<= 1
    return sorted(set(masks))


class KwinHotkeyListener:
    """Hold the KWin accessibility key grab and forward presses to a callback.

    The listener is constructed with a callable that fires once per
    physical Ctrl+Space press. We act on press events rather than
    release events because KWin only delivers a release KeyEvent when
    the modifier mask still matches one of the grabs at release time —
    if the user lifts Ctrl before Space, the Space release no longer
    matches ``Ctrl+Space`` and kwin drops it on the floor, leaving a
    release-driven state machine permanently stuck.

    KWin's per-mask fan-out is coalesced via a short dedupe window
    (``_PRESS_DEDUPE_WINDOW_S``): the first press in a window fires the
    callback, and any further press events arriving inside that window
    are treated as the same physical activation and ignored.

    The callback runs on the GLib main loop thread, so it must be quick
    and thread-safe; the daemon's ``toggle()`` is the intended target.
    """

    def __init__(
        self,
        on_activate: Callable[[], None],
        *,
        keysym: int = DEFAULT_HOTKEY_KEYSYM,
        required_modifier_mask: int = DEFAULT_REQUIRED_MODIFIER_MASK,
        ignored_modifier_mask: int = DEFAULT_IGNORED_MODIFIER_MASK,
        logger: logging.Logger | None = None,
        connection: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._on_activate = on_activate
        self._keysym = keysym
        self._required_modifier_mask = required_modifier_mask
        self._ignored_modifier_mask = ignored_modifier_mask
        self._logger = logger or configure_logging("kdictate.daemon.hotkey")
        self._connection = connection
        self._clock = clock
        self._owns_name = False
        self._signal_subscription: int | None = None
        # Sentinel: -inf means "no press yet seen", so the very first
        # press always fires regardless of clock starting value.
        self._last_press_time: float = float("-inf")
        self._masks = expand_modifier_masks(required_modifier_mask, ignored_modifier_mask)

    @property
    def masks(self) -> list[int]:
        """Return the modifier mask permutations registered with KWin."""
        return list(self._masks)

    def start(self) -> None:
        """Acquire the screen-reader name and install the KWin grab."""

        Gio, GLib = self._load_gi()
        if self._connection is None:
            self._connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)

        self._request_client_name(Gio, GLib)
        self._install_signal_subscription(Gio)
        self._set_key_grabs(Gio, GLib)
        self._logger.info(
            "Registered KWin hotkey grab keysym=0x%x masks=%s",
            self._keysym,
            [hex(mask) for mask in self._masks],
        )

    def stop(self) -> None:
        """Release the grab and the squatted name. Safe to call repeatedly,
        and safe to call after a partial start() failure — only the steps
        that actually completed are rolled back."""

        if self._connection is None:
            return
        Gio, GLib = self._load_gi()
        if self._signal_subscription is not None:
            try:
                self._connection.signal_unsubscribe(self._signal_subscription)
            except Exception as exc:  # noqa: BLE001
                self._logger.warning("failed to unsubscribe key signal: %s", exc)
            self._signal_subscription = None
        if self._owns_name:
            # Only clear key grabs if we ever owned the screen-reader name —
            # kwin rejects SetKeyGrabs from any other peer, so calling it
            # without owning the name just produces a noisy warning.
            try:
                self._call(
                    Gio,
                    GLib,
                    MONITOR_BUS_NAME,
                    MONITOR_OBJECT_PATH,
                    MONITOR_INTERFACE,
                    "SetKeyGrabs",
                    GLib.Variant("(aua(uu))", ([], [])),
                )
            except Exception as exc:  # noqa: BLE001
                self._logger.warning("failed to clear KWin key grabs: %s", exc)
            try:
                self._call(
                    Gio,
                    GLib,
                    "org.freedesktop.DBus",
                    "/org/freedesktop/DBus",
                    "org.freedesktop.DBus",
                    "ReleaseName",
                    GLib.Variant("(s)", (CLIENT_NAME,)),
                )
            except Exception as exc:  # noqa: BLE001
                self._logger.warning("failed to release %s: %s", CLIENT_NAME, exc)
            finally:
                self._owns_name = False

    # -- internals ----------------------------------------------------------

    def _request_client_name(self, Gio: Any, GLib: Any) -> None:
        # DO_NOT_QUEUE is essential — see the flag constant above for
        # the full rationale. Without it, flags=0 (or any flags without
        # 0x4 set) would cause dbus to queue us behind any existing
        # owner of CLIENT_NAME, and our failed-startup cleanup path
        # could not withdraw that queue entry cleanly.
        reply = self._call(
            Gio,
            GLib,
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "RequestName",
            GLib.Variant("(su)", (CLIENT_NAME, _DBUS_NAME_FLAG_DO_NOT_QUEUE)),
        )
        result_code = reply[0] if reply else None
        if result_code not in (
            _DBUS_REQUEST_NAME_PRIMARY_OWNER,
            _DBUS_REQUEST_NAME_ALREADY_OWNER,
        ):
            raise RuntimeError(
                f"Failed to own D-Bus name {CLIENT_NAME!r} (reply={result_code}). "
                "Another process — likely Orca or another instance of kdictate — "
                "owns the screen-reader keyboard monitor name."
            )
        self._owns_name = True
        self._logger.info("owned D-Bus name %s (reply=%s)", CLIENT_NAME, result_code)

    def _install_signal_subscription(self, Gio: Any) -> None:
        self._signal_subscription = self._connection.signal_subscribe(
            MONITOR_BUS_NAME,
            MONITOR_INTERFACE,
            "KeyEvent",
            MONITOR_OBJECT_PATH,
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_key_event,
        )

    def _set_key_grabs(self, Gio: Any, GLib: Any) -> None:
        keystrokes = [(self._keysym, mask) for mask in self._masks]
        self._call(
            Gio,
            GLib,
            MONITOR_BUS_NAME,
            MONITOR_OBJECT_PATH,
            MONITOR_INTERFACE,
            "SetKeyGrabs",
            GLib.Variant("(aua(uu))", ([], keystrokes)),
        )

    def _call(
        self,
        Gio: Any,
        GLib: Any,
        bus_name: str,
        object_path: str,
        interface_name: str,
        method: str,
        parameters: Any,
        timeout_ms: int = 5000,
    ) -> tuple:
        result = self._connection.call_sync(
            bus_name,
            object_path,
            interface_name,
            method,
            parameters,
            None,
            Gio.DBusCallFlags.NONE,
            timeout_ms,
            None,
        )
        return result.unpack() if result is not None else ()

    def _on_key_event(
        self,
        connection: Any,
        sender_name: str,
        object_path: str,
        interface_name: str,
        signal_name: str,
        parameters: Any,
        user_data: Any = None,
    ) -> None:
        del connection, sender_name, object_path, interface_name, signal_name, user_data
        try:
            unpacked = parameters.unpack()
        except AttributeError:
            unpacked = tuple(parameters)
        # KWin's KeyEvent payload: (released, state, keysym, unichar, keycode)
        released, state, keysym, _unichar, keycode = unpacked
        if keysym != self._keysym:
            return
        if released:
            # Releases are intentionally ignored. KWin only delivers a
            # release KeyEvent when the modifier mask still matches one
            # of our grabs at release time, so a Ctrl-then-Space release
            # order silently drops the release and would lock a release-
            # driven state machine.
            return
        now = self._clock()
        within_dedupe = (now - self._last_press_time) < _PRESS_DEDUPE_WINDOW_S
        # Trailing dedupe: every press event resets the window, even
        # the ones that get ignored. As long as new events keep
        # arriving inside the window, the gate stays closed. Without
        # this, hardware autorepeat (~25 Hz) would refire the
        # callback on every event because each one is "more than the
        # window after the last fire". With it, a held key keeps
        # rolling _last_press_time forward until the user actually
        # releases — at which point the next press lands more than
        # one window later and fires.
        self._last_press_time = now
        if within_dedupe:
            return
        self._logger.info("hotkey press state=0x%x keycode=%s", state, keycode)
        try:
            self._on_activate()
        except Exception:  # noqa: BLE001
            self._logger.exception("hotkey activation callback raised")

    def _load_gi(self) -> tuple[Any, Any]:
        try:
            import gi

            gi.require_version("Gio", "2.0")
            gi.require_version("GLib", "2.0")
            from gi.repository import Gio, GLib
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"PyGObject is required for the KWin hotkey listener: {exc}") from exc
        return Gio, GLib
