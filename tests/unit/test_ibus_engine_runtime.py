"""Tests for the runtime IBus bootstrap registration."""

from __future__ import annotations

from types import SimpleNamespace
import unittest

from whisper_dictate.ibus_engine import engine
from whisper_dictate.exceptions import IbusEngineError


class _FakeBus:
    def __init__(self, *, connected: bool = True, request_name_result: int = 1) -> None:
        self.connected = connected
        self.request_name_result = request_name_result
        self.connection = object()
        self.requested_names: list[tuple[str, int]] = []

    def is_connected(self) -> bool:
        return self.connected

    def get_connection(self) -> object:
        return self.connection

    def request_name(self, name: str, flags: int) -> int:
        self.requested_names.append((name, flags))
        return self.request_name_result


class _FakeFactory:
    def __init__(self, connection: object) -> None:
        self.connection = connection
        self.engines: list[tuple[str, object]] = []
        self.destroyed = False

    def add_engine(self, name: str, engine_type: object) -> None:
        self.engines.append((name, engine_type))

    def destroy(self) -> None:
        self.destroyed = True


class _FakeEngineBase:
    __gtype__ = object()


class IbusEngineRuntimeTests(unittest.TestCase):
    def _make_ibus_module(
        self,
        bus: _FakeBus | None = None,
    ) -> SimpleNamespace:
        active_bus = bus or _FakeBus()
        return SimpleNamespace(
            Bus=SimpleNamespace(new=lambda: active_bus),
            Factory=SimpleNamespace(new=lambda connection: _FakeFactory(connection)),
            Engine=_FakeEngineBase,
            BusRequestNameReply=SimpleNamespace(PRIMARY_OWNER=1, ALREADY_OWNER=4),
        )

    def test_initialize_runtime_claims_component_name_and_builds_factory(self) -> None:
        fake_bus = _FakeBus()
        fake_ibus = self._make_ibus_module(fake_bus)

        bus, factory = engine.initialize_engine_runtime(
            "/tmp/ibus-engine-whisper-dictate",
            ibus_module=fake_ibus,
        )

        self.assertIs(bus, fake_bus)
        self.assertEqual(fake_bus.requested_names, [(engine.COMPONENT_NAME, 0)])
        self.assertEqual(factory.connection, fake_bus.connection)
        self.assertEqual(factory.engines, [(engine.ENGINE_NAME, _FakeEngineBase.__gtype__)])

    def test_initialize_runtime_rejects_bus_connection_failure(self) -> None:
        fake_bus = _FakeBus(connected=False)
        fake_ibus = self._make_ibus_module(fake_bus)

        with self.assertRaisesRegex(IbusEngineError, "connect to the IBus bus"):
            engine.initialize_engine_runtime("/tmp/ibus-engine-whisper-dictate", ibus_module=fake_ibus)

        self.assertEqual(fake_bus.requested_names, [])

    def test_initialize_runtime_rejects_component_name_claim_failure(self) -> None:
        fake_bus = _FakeBus(request_name_result=3)
        fake_ibus = self._make_ibus_module(fake_bus)

        with self.assertRaisesRegex(IbusEngineError, "claim IBus component name"):
            engine.initialize_engine_runtime("/tmp/ibus-engine-whisper-dictate", ibus_module=fake_ibus)
