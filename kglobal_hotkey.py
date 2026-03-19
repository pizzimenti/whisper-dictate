"""KWin keyboard-monitor backend for whisper-dictate on Wayland.

Imported by dictate.py and runs inside the same process as the daemon.
HotkeyListener holds a reference to DictationDaemon and calls its methods
directly — no subprocess or IPC needed.

The GLib main loop in dictate.py drives the D-Bus event dispatch here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

if TYPE_CHECKING:
    from dictate import DictationDaemon

from dictate_runtime import STATE_IDLE, STATE_RECORDING, STATE_TRANSCRIBING


DEFAULT_CLIENT_NAME = "org.gnome.Orca.KeyboardMonitor"
DEFAULT_MONITOR_BUS_NAME = "org.freedesktop.a11y.Manager"
DEFAULT_MONITOR_OBJECT_PATH = "/org/freedesktop/a11y/Manager"
DEFAULT_MONITOR_INTERFACE = "org.freedesktop.a11y.KeyboardMonitor"

DEFAULT_HOTKEY_KEYSYM = 0x0020  # XK_space
DEFAULT_REQUIRED_MODIFIER_MASK = 0x04  # Control
DEFAULT_IGNORED_MODIFIER_MASK = 0x12  # CapsLock + NumLock

DBUS_REQUEST_NAME_PRIMARY_OWNER = 1
DBUS_REQUEST_NAME_ALREADY_OWNER = 4


def _log(message: str) -> None:
    print(message, flush=True)


def _expand_modifier_masks(required_mask: int, ignored_mask: int) -> list[int]:
    """Return the exact modifier masks KWin should treat as equivalent."""

    masks = [required_mask]
    bit = 1
    remaining = ignored_mask
    while remaining:
        if remaining & bit:
            masks.extend(existing | bit for existing in list(masks))
            remaining &= ~bit
        bit <<= 1
    return sorted(set(masks))


class HotkeyListener:
    """Own the KWin grab and map hotkey releases directly to daemon actions."""

    def __init__(
        self,
        daemon: DictationDaemon,
        *,
        client_name: str = DEFAULT_CLIENT_NAME,
        monitor_bus_name: str = DEFAULT_MONITOR_BUS_NAME,
        monitor_object_path: str = DEFAULT_MONITOR_OBJECT_PATH,
        monitor_interface: str = DEFAULT_MONITOR_INTERFACE,
        hotkey_keysym: int = DEFAULT_HOTKEY_KEYSYM,
        required_modifier_mask: int = DEFAULT_REQUIRED_MODIFIER_MASK,
        ignored_modifier_mask: int = DEFAULT_IGNORED_MODIFIER_MASK,
    ) -> None:
        self._daemon = daemon
        self._client_name = client_name
        self._monitor_bus_name = monitor_bus_name
        self._monitor_object_path = monitor_object_path
        self._monitor_interface = monitor_interface
        self._hotkey_keysym = hotkey_keysym
        self._hotkey_masks = _expand_modifier_masks(required_modifier_mask, ignored_modifier_mask)

        self.connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        # KWin fires one KeyEvent per registered modifier mask; track physical
        # key state to suppress duplicate events.
        self._key_held = False

    def _call(
        self,
        bus_name: str,
        object_path: str,
        interface_name: str,
        method: str,
        parameters: GLib.Variant | None,
        timeout_ms: int = 5000,
    ) -> tuple:
        result = self.connection.call_sync(
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

    def _request_name(self) -> None:
        reply = self._call(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "RequestName",
            GLib.Variant("(su)", (self._client_name, 0)),
        )
        result_code = reply[0]
        if result_code not in (DBUS_REQUEST_NAME_PRIMARY_OWNER, DBUS_REQUEST_NAME_ALREADY_OWNER):
            raise RuntimeError(
                f"Failed to own D-Bus name {self._client_name!r} (reply={result_code}). "
                "If Orca is running, stop it first."
            )
        _log(f"Owned D-Bus name {self._client_name!r} (reply={result_code}).")

    def register(self) -> None:
        """Own the expected D-Bus name and register the KWin key grabs."""

        self._request_name()

        keystrokes = [(self._hotkey_keysym, mask) for mask in self._hotkey_masks]
        self.connection.signal_subscribe(
            self._monitor_bus_name,
            self._monitor_interface,
            "KeyEvent",
            self._monitor_object_path,
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_key_event,
        )
        self._call(
            self._monitor_bus_name,
            self._monitor_object_path,
            self._monitor_interface,
            "SetKeyGrabs",
            GLib.Variant("(aua(uu))", ([], keystrokes)),
        )
        _log(
            "Registered KWin accessibility hotkey grabs for "
            f"keysym=0x{self._hotkey_keysym:x} modifier_masks={[hex(m) for m in self._hotkey_masks]}"
        )

    def _on_key_event(
        self,
        connection: Gio.DBusConnection,
        sender_name: str,
        object_path: str,
        interface_name: str,
        signal_name: str,
        parameters: GLib.Variant,
        user_data: object | None = None,
    ) -> None:
        del connection, sender_name, object_path, interface_name, signal_name, user_data
        released, state, keysym, _unichar, keycode = parameters.unpack()
        if keysym != self._hotkey_keysym:
            return

        if not released:
            return

        _log(f"Hotkey release. state=0x{state:x} keycode={keycode}")
        daemon_state = self._daemon.state
        if daemon_state == STATE_IDLE:
            self._daemon.request_start()
        elif daemon_state == STATE_RECORDING:
            self._daemon.request_stop()
        elif daemon_state == STATE_TRANSCRIBING:
            self._daemon._pending_start.set()
