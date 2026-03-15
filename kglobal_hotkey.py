#!/usr/bin/env python3

"""KWin keyboard-monitor backend for whisper-dictate on Wayland.

This process owns the accessibility-keyboard D-Bus name KWin expects and
translates a global hotkey release into terminal-control actions against the
long-lived dictation daemon.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib


DEFAULT_PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_CONTROL = DEFAULT_PROJECT_DIR / "dictatectl.py"

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


def _int_arg(value: str) -> int:
    return int(value, 0)


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


def parse_args() -> argparse.Namespace:
    """Parse listener configuration for KWin and dictatectl integration."""

    parser = argparse.ArgumentParser(
        description="Listen for a global whisper-dictate hotkey via KWin's Wayland accessibility keyboard monitor."
    )
    parser.add_argument(
        "--client-name",
        default=DEFAULT_CLIENT_NAME,
        help="D-Bus service name to own before talking to KWin's keyboard monitor.",
    )
    parser.add_argument(
        "--monitor-bus-name",
        default=DEFAULT_MONITOR_BUS_NAME,
        help="D-Bus service that exposes org.freedesktop.a11y.KeyboardMonitor.",
    )
    parser.add_argument(
        "--monitor-object-path",
        default=DEFAULT_MONITOR_OBJECT_PATH,
        help="D-Bus object path for the keyboard monitor.",
    )
    parser.add_argument(
        "--monitor-interface",
        default=DEFAULT_MONITOR_INTERFACE,
        help="D-Bus interface for the keyboard monitor.",
    )
    parser.add_argument(
        "--hotkey-keysym",
        type=_int_arg,
        default=DEFAULT_HOTKEY_KEYSYM,
        help="XKB keysym for the toggle hotkey. Defaults to XK_space.",
    )
    parser.add_argument(
        "--required-modifier-mask",
        type=_int_arg,
        default=DEFAULT_REQUIRED_MODIFIER_MASK,
        help="Exact XKB modifier mask required for the hotkey. Defaults to Control.",
    )
    parser.add_argument(
        "--ignored-modifier-mask",
        type=_int_arg,
        default=DEFAULT_IGNORED_MODIFIER_MASK,
        help="Additional lock-style modifier bits to tolerate, such as CapsLock and NumLock.",
    )
    parser.add_argument(
        "--control-script",
        default=str(DEFAULT_CONTROL),
        help="Path to dictatectl.py.",
    )
    return parser.parse_args()


class HotkeyListener:
    """Own the KWin grab and map hotkey releases to daemon actions."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.hotkey_masks = _expand_modifier_masks(args.required_modifier_mask, args.ignored_modifier_mask)
        # KWin fires one KeyEvent per registered modifier mask, so track physical
        # key state here to suppress the duplicate events.
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
            GLib.Variant("(su)", (self.args.client_name, 0)),
        )
        result_code = reply[0]
        if result_code not in (DBUS_REQUEST_NAME_PRIMARY_OWNER, DBUS_REQUEST_NAME_ALREADY_OWNER):
            raise RuntimeError(
                f"Failed to own D-Bus name {self.args.client_name!r} (reply={result_code}). "
                "If Orca is running, stop it first."
            )
        _log(f"Owned D-Bus name {self.args.client_name!r} (reply={result_code}).")

    def register(self) -> None:
        """Own the expected D-Bus name and register the KWin key grabs."""

        self._request_name()

        keystrokes = [(self.args.hotkey_keysym, mask) for mask in self.hotkey_masks]
        self.connection.signal_subscribe(
            self.args.monitor_bus_name,
            self.args.monitor_interface,
            "KeyEvent",
            self.args.monitor_object_path,
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_key_event,
        )
        self._call(
            self.args.monitor_bus_name,
            self.args.monitor_object_path,
            self.args.monitor_interface,
            "SetKeyGrabs",
            GLib.Variant("(aua(uu))", ([], keystrokes)),
        )
        _log(
            "Registered KWin accessibility hotkey grabs for "
            f"keysym=0x{self.args.hotkey_keysym:x} modifier_masks={[hex(mask) for mask in self.hotkey_masks]}"
        )

    def _run_control(self, *command: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, self.args.control_script, *command],
            check=False,
            capture_output=True,
            text=True,
        )

    def _daemon_state(self) -> str:
        result = self._run_control("status")
        if result.returncode != 0:
            return "unknown"
        return result.stdout.strip()

    def _start_dictation(self) -> None:
        result = self._run_control("start")
        if result.returncode != 0:
            _log(f"Start failed: {result.stderr.strip()}")
            return
        _log("Dictation started.")

    def _stop_dictation(self) -> None:
        result = self._run_control("stop", "--no-wait")
        if result.returncode != 0:
            _log(f"Stop failed: {result.stderr.strip()}")

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
        if keysym != self.args.hotkey_keysym:
            return

        if not released:
            if self._key_held:
                return
            self._key_held = True
            _log(f"Hotkey press. state=0x{state:x} keycode={keycode}")
            state_name = self._daemon_state()
            if state_name != "recording":
                self._start_dictation()
        else:
            if not self._key_held:
                return
            self._key_held = False
            _log(f"Hotkey release. state=0x{state:x} keycode={keycode}")
            state_name = self._daemon_state()
            if state_name == "recording":
                self._stop_dictation()


def main() -> int:
    """Create the listener, register the grab, and keep the GLib loop alive."""

    args = parse_args()
    listener = HotkeyListener(args)
    try:
        listener.register()
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to register KWin accessibility hotkey listener: {exc}", file=sys.stderr)
        return 1

    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
