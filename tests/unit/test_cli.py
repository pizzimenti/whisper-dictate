from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

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
