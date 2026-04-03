from __future__ import annotations

import unittest

from whisper_dictate.exceptions import DbusServiceError
from whisper_dictate.service.dbus_service import SessionDbusService


class _Backend:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.state = "idle"
        self.last_text = "hello"

    def request_start(self) -> None:
        self.calls.append("start")
        self.state = "recording"

    def request_stop(self) -> None:
        self.calls.append("stop")
        self.state = "idle"

    def toggle(self) -> None:
        self.calls.append("toggle")

    def get_state(self) -> str:
        self.calls.append("get_state")
        return self.state

    def get_last_text(self) -> str:
        self.calls.append("get_last_text")
        return self.last_text

    def ping(self) -> str:
        self.calls.append("ping")
        return "pong"


class _Invocation:
    def __init__(self) -> None:
        self.value = None
        self.error = None

    def return_value(self, value) -> None:
        self.value = value

    def return_dbus_error(self, name: str, message: str) -> None:
        self.error = (name, message)


class SessionDbusServiceTest(unittest.TestCase):
    def test_dispatch_and_signal_forwarding(self) -> None:
        signals: list[tuple[str, tuple[object, ...]]] = []
        backend = _Backend()
        service = SessionDbusService(backend, signal_sender=lambda name, params: signals.append((name, params)))

        self.assertEqual(service._dispatch("GetState"), ("idle",))
        self.assertEqual(service._dispatch("GetLastText"), ("hello",))
        self.assertEqual(service._dispatch("Ping"), ("pong",))
        self.assertEqual(service._dispatch("Start"), ())
        self.assertEqual(service._dispatch("Stop"), ())
        self.assertEqual(service._dispatch("Toggle"), ())

        service.state_changed("recording")
        service.partial_transcript("partial text")
        service.final_transcript("final text")
        service.error_occurred("code", "message")

        self.assertEqual(
            signals,
            [
                ("StateChanged", ("recording",)),
                ("PartialTranscript", ("partial text",)),
                ("FinalTranscript", ("final text",)),
                ("ErrorOccurred", ("code", "message")),
            ],
        )

    def test_method_call_returns_variant_and_surfaces_errors(self) -> None:
        backend = _Backend()
        service = SessionDbusService(backend, signal_sender=lambda name, params: None)
        invocation = _Invocation()

        service._on_method_call(None, "", "", "", "GetState", None, invocation)
        self.assertIsNotNone(invocation.value)
        self.assertEqual(invocation.value.unpack(), ("idle",))

        invocation = _Invocation()
        service._on_method_call(None, "", "", "", "Start", None, invocation)
        self.assertIsNone(invocation.error)
        self.assertIn("start", backend.calls)

        invocation = _Invocation()
        backend.get_state = lambda: (_ for _ in ()).throw(DbusServiceError("boom"))  # type: ignore[assignment]
        service._on_method_call(None, "", "", "", "GetState", None, invocation)
        self.assertIsNotNone(invocation.error)
        self.assertIsInstance(invocation.error[1], str)

    def test_dispatch_rejects_unknown_methods(self) -> None:
        service = SessionDbusService(_Backend(), signal_sender=lambda name, params: None)
        with self.assertRaises(DbusServiceError):
            service._dispatch("Unknown")
