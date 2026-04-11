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
from typing import Any, Callable, Iterable

from kdictate.logging_utils import configure_logging

# Defaults match the historical kglobal_hotkey.py listener: Ctrl+Space.
DEFAULT_HOTKEY_KEYSYM = 0x0020  # XK_space
DEFAULT_REQUIRED_MODIFIER_MASK = 0x04  # Control
DEFAULT_IGNORED_MODIFIER_MASK = 0x12  # CapsLock + NumLock

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
    """Hold the KWin accessibility key grab and forward releases to a callback.

    The listener is constructed with a callable that runs whenever the
    grabbed key is *released* (release-only mirrors the original
    behavior — press triggers nothing). The callable runs on the GLib
    main loop thread, so it must be quick and thread-safe; the daemon's
    ``toggle()`` is the intended target.
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
    ) -> None:
        self._on_release = on_release
        self._keysym = keysym
        self._required_modifier_mask = required_modifier_mask
        self._ignored_modifier_mask = ignored_modifier_mask
        self._logger = logger or configure_logging("kdictate.hotkey")
        self._connection = connection
        self._owns_name = False
        self._signal_subscription: int | None = None
        self._key_held = False
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
        """Release the grab and the squatted name. Safe to call repeatedly."""

        if self._connection is None:
            return
        Gio, GLib = self._load_gi()
        if self._signal_subscription is not None:
            try:
                self._connection.signal_unsubscribe(self._signal_subscription)
            except Exception as exc:  # noqa: BLE001
                self._logger.warning("failed to unsubscribe key signal: %s", exc)
            self._signal_subscription = None
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
        if self._owns_name:
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
        if not released:
            if self._key_held:
                return
            self._key_held = True
            return
        if not self._key_held:
            return
        self._key_held = False
        self._logger.info("hotkey release state=0x%x keycode=%s", state, keycode)
        try:
            self._on_release()
        except Exception:  # noqa: BLE001
            self._logger.exception("hotkey release callback raised")

    def _load_gi(self) -> tuple[Any, Any]:
        try:
            import gi

            gi.require_version("Gio", "2.0")
            gi.require_version("GLib", "2.0")
            from gi.repository import Gio, GLib
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"PyGObject is required for the KWin hotkey listener: {exc}") from exc
        return Gio, GLib
