from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from contextlib import redirect_stderr

from whisper_dictate.exceptions import DbusServiceError
from whisper_dictate.cli.dictatectl import DbusControlClient, _handle_start, _handle_stop, _handle_toggle


class _FakeClient:
    def __init__(self, state: str = "idle", last_text: str = "") -> None:
        self.state = state
        self.last_text = last_text
        self.calls: list[str] = []

    def get_state(self) -> str:
        return self.state

    def get_last_text(self) -> str:
        return self.last_text

    def start(self) -> None:
        self.calls.append("start")
        self.state = "recording"

    def stop(self) -> None:
        self.calls.append("stop")
        self.state = "idle"

    def toggle(self) -> None:
        self.calls.append("toggle")
        self.state = "recording" if self.state == "idle" else "idle"


class _SequencedStateClient(_FakeClient):
    def __init__(self, states: list[str], last_text: str = "") -> None:
        super().__init__(state=states[0] if states else "idle", last_text=last_text)
        self._states = states
        self._index = 0

    def get_state(self) -> str:
        if self._index < len(self._states):
            self.state = self._states[self._index]
            self._index += 1
        return self.state

    def start(self) -> None:
        self.calls.append("start")


class CliTest(unittest.TestCase):
    def test_dbus_client_wraps_scalar_results(self) -> None:
        client = DbusControlClient(call_sync=lambda method, params: "idle" if method == "GetState" else "pong")
        self.assertEqual(client.get_state(), "idle")
        self.assertEqual(client.ping(), "pong")

    def test_start_stop_and_toggle_around_fake_client(self) -> None:
        client = _FakeClient(state="idle", last_text="hello")

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = _handle_start(client, timeout=0.1, wait=True)
        self.assertEqual(rc, 0)
        self.assertIn("recording", buf.getvalue())
        self.assertIn("start", client.calls)

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = _handle_stop(client, timeout=0.1, wait=True)
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().strip(), "hello")
        self.assertIn("stop", client.calls)

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = _handle_toggle(client, timeout=0.1, wait=True)
        self.assertEqual(rc, 0)
        self.assertIn("recording", buf.getvalue())

    def test_stop_during_starting_state_sends_stop(self) -> None:
        client = _FakeClient(state="starting")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = _handle_stop(client, timeout=0.1, wait=True)
        self.assertEqual(rc, 0)
        self.assertIn("stop", client.calls)

    def test_toggle_during_starting_state_sends_stop(self) -> None:
        client = _FakeClient(state="starting")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = _handle_toggle(client, timeout=0.1, wait=True)
        self.assertEqual(rc, 0)
        self.assertIn("stop", client.calls)

    def test_start_during_starting_state_is_noop(self) -> None:
        client = _FakeClient(state="starting")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = _handle_start(client, timeout=0.1, wait=True)
        self.assertEqual(rc, 0)
        self.assertIn("starting", buf.getvalue())
        self.assertEqual(client.calls, [])

    def test_start_fails_fast_when_daemon_returns_idle_after_starting(self) -> None:
        client = _SequencedStateClient(["idle", "starting", "idle"])
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = _handle_start(client, timeout=5.0, wait=True)
        self.assertEqual(rc, 1)
        self.assertIn("start", client.calls)
        self.assertIn("Recording failed to start.", stderr.getvalue())

    def test_start_fails_fast_when_daemon_returns_error(self) -> None:
        client = _SequencedStateClient(["idle", "starting", "error"])
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = _handle_start(client, timeout=5.0, wait=True)
        self.assertEqual(rc, 1)
        self.assertIn("start", client.calls)
        self.assertIn("Recording failed to start.", stderr.getvalue())

    def test_dbus_client_translates_transport_errors(self) -> None:
        class FakeProxy:
            def call_sync(self, *args, **kwargs):
                raise RuntimeError("unavailable")

        class FakeGio:
            class DBusCallFlags:
                NONE = object()

        class FakeGLib:
            Variant = staticmethod(lambda _sig, value: value)

        client = DbusControlClient()
        client._proxy = FakeProxy()
        client._load_gi = lambda: (FakeGio, FakeGLib)  # type: ignore[method-assign]

        with self.assertRaises(DbusServiceError):
            client.get_state()
