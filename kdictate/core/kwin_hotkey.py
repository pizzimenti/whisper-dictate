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

# KWin emits one KeyEvent per registered modifier mask permutation, so a
# single physical Ctrl+Space press fans out into 4 events that all arrive
# within microseconds of each other. Coalesce them into one activation by
# rejecting press events that arrive within this window of the previous
# accepted press. Set well above the kwin fan-out latency (~1ms) and well
# below realistic intentional double-tap timing (~200ms).
_PRESS_DEDUPE_WINDOW_S = 0.020

CLIENT_NAME = "org.gnome.Orca.KeyboardMonitor"
MONITOR_BUS_NAME = "org.freedesktop.a11y.Manager"
MONITOR_OBJECT_PATH = "/org/freedesktop/a11y/Manager"
MONITOR_INTERFACE = "org.freedesktop.a11y.KeyboardMonitor"

_DBUS_REQUEST_NAME_PRIMARY_OWNER = 1
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
        on_release: Callable[[], None],
        *,
        keysym: int = DEFAULT_HOTKEY_KEYSYM,
        required_modifier_mask: int = DEFAULT_REQUIRED_MODIFIER_MASK,
        ignored_modifier_mask: int = DEFAULT_IGNORED_MODIFIER_MASK,
        logger: logging.Logger | None = None,
        connection: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # Parameter is named ``on_release`` for backwards compatibility
        # with the previous release-driven implementation; semantically
        # it now fires on press.
        self._on_activate = on_release
        self._keysym = keysym
        self._required_modifier_mask = required_modifier_mask
        self._ignored_modifier_mask = ignored_modifier_mask
        self._logger = logger or configure_logging("kdictate.hotkey")
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
        reply = self._call(
            Gio,
            GLib,
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "RequestName",
            GLib.Variant("(su)", (CLIENT_NAME, 0)),
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
        if now - self._last_press_time < _PRESS_DEDUPE_WINDOW_S:
            # Per-mask fan-out from the same physical press. Same
            # physical activation, no-op.
            return
        self._last_press_time = now
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
