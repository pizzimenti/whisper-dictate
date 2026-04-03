"""Session D-Bus terminal control helper for whisper-dictate."""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any, Callable, Sequence

from whisper_dictate.constants import DBUS_BUS_NAME, DBUS_INTERFACE, DBUS_OBJECT_PATH
from whisper_dictate.exceptions import DbusServiceError
from whisper_dictate.logging_utils import configure_logging
from whisper_dictate.runtime import STATE_ERROR, STATE_IDLE, STATE_RECORDING, STATE_STARTING, STATE_TRANSCRIBING


class DbusControlClient:
    """Thin D-Bus client wrapper used by the CLI."""

    def __init__(
        self,
        *,
        bus_name: str = DBUS_BUS_NAME,
        object_path: str = DBUS_OBJECT_PATH,
        interface_name: str = DBUS_INTERFACE,
        call_sync: Callable[[str, tuple[Any, ...] | None], tuple[Any, ...] | None] | None = None,
    ) -> None:
        self._bus_name = bus_name
        self._object_path = object_path
        self._interface_name = interface_name
        self._call_sync = call_sync
        self._proxy = None

    def _load_gi(self):
        """Import Gio/GLib lazily for runtime and tests."""

        try:
            import gi

            gi.require_version("Gio", "2.0")
            gi.require_version("GLib", "2.0")
            from gi.repository import Gio, GLib
        except Exception as exc:  # noqa: BLE001
            raise DbusServiceError(f"PyGObject is required for the CLI: {exc}") from exc
        return Gio, GLib

    def _ensure_proxy(self):
        """Create a Gio.DBusProxy on first use."""

        if self._proxy is not None or self._call_sync is not None:
            return self._proxy
        Gio, _GLib = self._load_gi()
        try:
            connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self._proxy = Gio.DBusProxy.new_sync(
                connection,
                Gio.DBusProxyFlags.NONE,
                None,
                self._bus_name,
                self._object_path,
                self._interface_name,
                None,
            )
        except Exception as exc:  # noqa: BLE001
            raise DbusServiceError(
                f"Unable to connect to {self._bus_name} on the session bus: {exc}"
            ) from exc
        return self._proxy

    def call(self, method_name: str) -> tuple[Any, ...]:
        """Invoke a D-Bus method and normalize the return shape."""

        if self._call_sync is not None:
            result = self._call_sync(method_name, None)
            if result is None:
                return ()
            if isinstance(result, tuple):
                return result
            return (result,)

        proxy = self._ensure_proxy()
        Gio, GLib = self._load_gi()
        if method_name in {"Start", "Stop", "Toggle"}:
            variant = None
        else:
            variant = GLib.Variant("()", ())
        try:
            result = proxy.call_sync(
                method_name,
                variant,
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
        except Exception as exc:  # noqa: BLE001
            raise DbusServiceError(
                f"D-Bus method {method_name} failed against {self._bus_name}: {exc}"
            ) from exc
        return tuple(result.unpack() if result is not None else ())

    def start(self) -> None:
        """Request recording start."""

        self.call("Start")

    def stop(self) -> None:
        """Request recording stop."""

        self.call("Stop")

    def toggle(self) -> None:
        """Toggle recording."""

        self.call("Toggle")

    def get_state(self) -> str:
        """Return the daemon state."""

        result = self.call("GetState")
        return result[0] if result else STATE_IDLE

    def get_last_text(self) -> str:
        """Return the latest finalized transcript."""

        result = self.call("GetLastText")
        return result[0] if result else ""

    def ping(self) -> str:
        """Return the daemon liveness marker."""

        result = self.call("Ping")
        return result[0] if result else "pong"


def build_parser() -> argparse.ArgumentParser:
    """Construct the control CLI parser."""

    parser = argparse.ArgumentParser(description="Terminal control for whisper-dictate.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="Print the current daemon state.")
    subparsers.add_parser("last-text", help="Print the latest transcript.")
    start = subparsers.add_parser("start", help="Start recording.")
    start.add_argument("--wait", action=argparse.BooleanOptionalAction, default=True)
    start.add_argument("--timeout", type=float, default=5.0)
    stop = subparsers.add_parser("stop", help="Stop recording.")
    stop.add_argument("--wait", action=argparse.BooleanOptionalAction, default=True)
    stop.add_argument("--timeout", type=float, default=20.0)
    toggle = subparsers.add_parser("toggle", help="Toggle recording state.")
    toggle.add_argument("--wait", action=argparse.BooleanOptionalAction, default=True)
    toggle.add_argument("--timeout", type=float, default=20.0)
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse control CLI arguments."""

    return build_parser().parse_args(argv)


def _print_last_text(text: str) -> int:
    if text:
        print(text)
    else:
        print("(no transcript)", file=sys.stderr)
    return 0


def _wait_for_state(client: DbusControlClient, targets: set[str], timeout: float) -> str | None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.get_state()
        if state in targets:
            return state
        time.sleep(0.15)
    state = client.get_state()
    return state if state in targets else None


def _wait_for_start_outcome(client: DbusControlClient, timeout: float) -> str | None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.get_state()
        if state in {STATE_RECORDING, STATE_TRANSCRIBING, STATE_IDLE, STATE_ERROR}:
            return state
        time.sleep(0.15)
    return client.get_state()


def _handle_start(client: DbusControlClient, timeout: float, wait: bool) -> int:
    state = client.get_state()
    if state in {STATE_RECORDING, STATE_STARTING}:
        print(state)
        return 0
    if state == STATE_TRANSCRIBING:
        print(STATE_TRANSCRIBING, file=sys.stderr)
        return 1
    client.start()
    if not wait:
        print("starting")
        return 0
    new_state = _wait_for_start_outcome(client, timeout)
    if new_state in {STATE_RECORDING, STATE_TRANSCRIBING}:
        print(new_state)
        return 0
    if new_state == STATE_ERROR:
        print("Recording failed to start.", file=sys.stderr)
        return 1
    if new_state == STATE_IDLE:
        print("Recording failed to start.", file=sys.stderr)
        return 1
    if new_state is None:
        print("Timed out waiting for recording to start.", file=sys.stderr)
        return 1
    print(f"Unexpected daemon state while starting: {new_state}", file=sys.stderr)
    return 1


def _handle_stop(client: DbusControlClient, timeout: float, wait: bool) -> int:
    state = client.get_state()
    if state == STATE_IDLE:
        return _print_last_text(client.get_last_text())
    if state == STATE_STARTING:
        client.stop()
        if not wait:
            print("stopping")
            return 0
        new_state = _wait_for_state(client, {STATE_IDLE}, timeout)
        if new_state is None:
            print("Timed out waiting for startup cancellation.", file=sys.stderr)
            return 1
        return _print_last_text(client.get_last_text())
    client.stop()
    if not wait:
        print("stopping")
        return 0
    new_state = _wait_for_state(client, {STATE_IDLE}, timeout)
    if new_state is None:
        print("Timed out waiting for transcription to finish.", file=sys.stderr)
        return 1
    return _print_last_text(client.get_last_text())


def _handle_toggle(client: DbusControlClient, timeout: float, wait: bool) -> int:
    state = client.get_state()
    if state in {STATE_RECORDING, STATE_STARTING}:
        return _handle_stop(client, timeout, wait)
    if state == STATE_TRANSCRIBING:
        client.toggle()
        if not wait:
            print("toggling")
            return 0
        new_state = _wait_for_state(client, {STATE_IDLE, STATE_RECORDING, STATE_TRANSCRIBING}, timeout)
        return 0 if new_state is not None else 1
    return _handle_start(client, timeout, wait)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the control helper."""

    logger = configure_logging("whisper_dictate.cli")
    args = parse_args(argv)
    client = DbusControlClient()
    try:
        if args.command == "status":
            print(client.get_state())
            return 0
        if args.command == "last-text":
            return _print_last_text(client.get_last_text())
        if args.command == "start":
            return _handle_start(client, args.timeout, args.wait)
        if args.command == "stop":
            return _handle_stop(client, args.timeout, args.wait)
        if args.command == "toggle":
            return _handle_toggle(client, args.timeout, args.wait)
    except DbusServiceError as exc:
        logger.error("%s", exc)
        print(str(exc), file=sys.stderr)
        return 1
    return 0
