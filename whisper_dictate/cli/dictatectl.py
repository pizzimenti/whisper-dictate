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

_logger = logging.getLogger("whisper_dictate.cli")


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

    def wait_for_state(self, targets: set[str], timeout: float) -> str | None:
        """Wait until the daemon state enters ``targets`` or ``timeout`` elapses.

        Production path: subscribe to the daemon's ``StateChanged`` signal
        on the session bus and wake on actual transitions instead of issuing
        a ``GetState`` D-Bus round-trip every 150 ms (which can be ~133 calls
        for a 20-second stop wait).

        Test path: when ``_call_sync`` is injected (no real Gio connection),
        fall back to a 150 ms polling loop so unit tests with synthesized
        fakes keep working without spinning up a fake D-Bus.
        """

        if self._call_sync is not None:
            return self._poll_for_state(targets, timeout)
        return self._signal_wait_for_state(targets, timeout)

    def _poll_for_state(self, targets: set[str], timeout: float) -> str | None:
        """Polling fallback used when no real D-Bus connection is available."""

        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.get_state()
            if state in targets:
                return state
            time.sleep(0.15)
        state = self.get_state()
        return state if state in targets else None

    def _signal_wait_for_state(self, targets: set[str], timeout: float) -> str | None:
        """Signal-subscription wait used in production."""

        Gio, GLib = self._load_gi()

        # Reuse the proxy's existing session bus connection instead of opening
        # a second one alongside it. _ensure_proxy is a no-op on subsequent
        # calls; the first call also walks the introspection path so the
        # proxy is fully initialized before we subscribe.
        proxy = self._ensure_proxy()
        if proxy is None:
            # Should not happen on the production path (we already returned
            # early in wait_for_state when _call_sync was injected), but be
            # defensive: fall back to opening a fresh connection.
            try:
                connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            except Exception as exc:  # noqa: BLE001
                raise DbusServiceError(
                    f"Unable to acquire session bus to wait for state: {exc}"
                ) from exc
        else:
            connection = proxy.get_connection()

        loop = GLib.MainLoop()
        result: dict[str, str | None] = {"state": None}

        def _on_state_signal(
            _connection: Any,
            _sender: str,
            _object_path: str,
            _interface_name: str,
            signal_name: str,
            params: Any,
            _user_data: Any = None,
        ) -> None:
            if signal_name != "StateChanged":
                return
            try:
                state = params.unpack()[0]
            except Exception:  # noqa: BLE001
                return
            if state in targets:
                result["state"] = state
                loop.quit()

        sub_id = connection.signal_subscribe(
            self._bus_name,
            self._interface_name,
            "StateChanged",
            self._object_path,
            None,
            Gio.DBusSignalFlags.NONE,
            _on_state_signal,
            None,
        )

        timeout_source_id: int | None = None
        try:
            # Race-fix: a transition into the target set may have already
            # happened between the caller's last get_state() and our signal
            # subscription. Probe once after subscribing so we never block
            # for the full timeout on a state we already reached.
            current = self.get_state()
            if current in targets:
                return current

            timeout_ms = max(1, int(timeout * 1000))

            def _on_timeout() -> bool:
                loop.quit()
                return False  # GLib.SOURCE_REMOVE

            timeout_source_id = GLib.timeout_add(timeout_ms, _on_timeout)
            loop.run()
        finally:
            connection.signal_unsubscribe(sub_id)
            if timeout_source_id is not None:
                # If the signal arrived before the timeout, the timeout source
                # is still pending on the GLib main context and would otherwise
                # leak there until it eventually self-removes. Cancel it
                # explicitly. If it has already fired, source_remove raises;
                # log at debug level so unexpected failures are still
                # observable in production but the expected already-fired
                # case is not noisy.
                try:
                    GLib.source_remove(timeout_source_id)
                except Exception as exc:  # noqa: BLE001
                    _logger.debug(
                        "GLib.source_remove(%s) suppressed: %s",
                        timeout_source_id,
                        exc,
                    )

        return result["state"]


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


_START_OUTCOME_TARGETS = frozenset({STATE_RECORDING, STATE_TRANSCRIBING, STATE_IDLE, STATE_ERROR})


def _wait_for_state(client: Any, targets: set[str], timeout: float) -> str | None:
    """Wait for the daemon to reach one of ``targets`` or for ``timeout``.

    Prefers ``client.wait_for_state`` when the client provides it (the real
    DbusControlClient uses session-bus signal subscription). Falls back to a
    150 ms polling loop for test fakes that don't implement signal
    subscription.
    """

    waiter = getattr(client, "wait_for_state", None)
    if waiter is not None:
        return waiter(set(targets), timeout)

    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.get_state()
        if state in targets:
            return state
        time.sleep(0.15)
    state = client.get_state()
    return state if state in targets else None


def _wait_for_start_outcome(client: Any, timeout: float) -> str | None:
    """Wait for the daemon to leave STATE_STARTING."""

    waiter = getattr(client, "wait_for_state", None)
    if waiter is not None:
        result = waiter(set(_START_OUTCOME_TARGETS), timeout)
        # The signal-subscription path returns None on timeout; preserve the
        # original behavior of falling back to one final get_state() probe so
        # the caller can surface a meaningful "Unexpected daemon state"
        # message instead of a generic timeout.
        if result is not None:
            return result
        return client.get_state()

    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.get_state()
        if state in _START_OUTCOME_TARGETS:
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
    if new_state in {STATE_ERROR, STATE_IDLE}:
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
        # Toggle while transcribing sets _pending_start on the daemon: it
        # finishes draining transcription, transitions through IDLE, and
        # then immediately re-enters RECORDING. Wait for the post-deferred
        # state — IDLE means the deferred start was dropped, RECORDING
        # means it succeeded. STATE_TRANSCRIBING is intentionally NOT in
        # the wait set; including it would cause the race-fix probe to
        # match the current state and return rc=0 before the deferred
        # start has actually been honored.
        new_state = _wait_for_state(client, {STATE_IDLE, STATE_RECORDING}, timeout)
        if new_state is None:
            print("Timed out waiting for toggle to resolve.", file=sys.stderr)
            return 1
        print(new_state)
        return 0
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
