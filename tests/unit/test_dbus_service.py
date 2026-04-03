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


class _FakeVariant:
    def __init__(self, value):
        self._value = value

    def unpack(self):
        return self._value


class _FakeVariantType:
    def __init__(self, signature: str) -> None:
        self.signature = signature


class _FakeNodeInfo:
    def __init__(self) -> None:
        self.interfaces = ["iface"]


class _FakeConnection:
    def __init__(self, request_name_reply: int = 1) -> None:
        self.request_name_reply = request_name_reply
        self.registered: list[tuple] = []
        self.unregistered: list[int] = []
        self.calls: list[tuple] = []

    def register_object(self, *args):
        self.registered.append(args)
        return 99

    def unregister_object(self, registration_id: int) -> None:
        self.unregistered.append(registration_id)

    def call_sync(self, *args):
        self.calls.append(args)
        if args[3] == "RequestName":
            return _FakeVariant((self.request_name_reply,))
        if args[3] == "ReleaseName":
            return _FakeVariant((1,))
        raise AssertionError(f"Unexpected method call: {args[3]}")


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

    def test_start_requests_bus_name_synchronously(self) -> None:
        service = SessionDbusService(_Backend(), signal_sender=lambda name, params: None)
        connection = _FakeConnection()

        class FakeGio:
            class BusType:
                SESSION = object()

            class DBusCallFlags:
                NONE = object()

            class DBusNodeInfo:
                @staticmethod
                def new_for_xml(_xml: str):
                    return _FakeNodeInfo()

            @staticmethod
            def bus_get_sync(_bus_type, _cancellable):
                return connection

        class FakeGLib:
            Variant = staticmethod(lambda _sig, value: _FakeVariant(value))
            VariantType = _FakeVariantType

        service._load_gi = lambda: (FakeGio, FakeGLib)  # type: ignore[method-assign]
        service.start()

        self.assertTrue(service._owns_bus_name)
        self.assertEqual(connection.calls[0][3], "RequestName")

    def test_start_raises_when_bus_name_is_not_owned(self) -> None:
        service = SessionDbusService(_Backend(), signal_sender=lambda name, params: None)
        connection = _FakeConnection(request_name_reply=2)

        class FakeGio:
            class BusType:
                SESSION = object()

            class DBusCallFlags:
                NONE = object()

            class DBusNodeInfo:
                @staticmethod
                def new_for_xml(_xml: str):
                    return _FakeNodeInfo()

            @staticmethod
            def bus_get_sync(_bus_type, _cancellable):
                return connection

        class FakeGLib:
            Variant = staticmethod(lambda _sig, value: _FakeVariant(value))
            VariantType = _FakeVariantType

        service._load_gi = lambda: (FakeGio, FakeGLib)  # type: ignore[method-assign]

        with self.assertRaises(DbusServiceError):
            service.start()
