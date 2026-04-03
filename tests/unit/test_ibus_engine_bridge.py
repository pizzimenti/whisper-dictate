from __future__ import annotations

import logging
import unittest

from whisper_dictate.constants import STATE_RECORDING
from whisper_dictate.ibus_engine.controller import DictationEngineController
from whisper_dictate.ibus_engine.dbus_client import DaemonControlBridge, DaemonSignalBridge


class FakeVariant:
    def __init__(self, values: tuple[object, ...]) -> None:
        self._values = values

    def unpack(self) -> tuple[object, ...]:
        return self._values


class FakeConnection:
    def __init__(self) -> None:
        self.subscriptions: list[tuple] = []
        self.unsubscribed: list[int] = []
        self.calls: list[tuple] = []
        self._next_id = 1

    def signal_subscribe(self, *args):
        self.subscriptions.append(args)
        current = self._next_id
        self._next_id += 1
        return current

    def signal_unsubscribe(self, subscription_id: int) -> None:
        self.unsubscribed.append(subscription_id)

    def call_sync(self, *args):
        self.calls.append(args)
        return FakeVariant((STATE_RECORDING,))


class FakeAdapter:
    def __init__(self) -> None:
        self.actions: list[tuple[str, object]] = []

    def update_preedit(self, text: str, *, visible: bool, focus_mode: str) -> None:
        self.actions.append(("update_preedit", text, visible, focus_mode))

    def commit_text(self, text: str) -> None:
        self.actions.append(("commit_text", text))


class DaemonSignalBridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = FakeAdapter()
        self.logger = logging.getLogger("whisper_dictate.tests")
        self.controller = DictationEngineController(self.adapter, self.logger)
        self.connection = FakeConnection()
        self.bridge = DaemonSignalBridge(
            self.controller,
            self.logger,
            watch_name=lambda *args: 42,
            unwatch_name=lambda _watch_id: None,
        )

    def test_name_appeared_subscribes_and_seeds_state(self) -> None:
        self.bridge._on_name_appeared(self.connection, "name", "owner")

        self.assertTrue(self.controller.state.daemon_available)
        self.assertEqual(len(self.connection.subscriptions), 4)
        self.assertEqual(self.connection.calls[0][2], "io.github.pizzimenti.WhisperDictate1")
        self.assertEqual(self.controller.state.daemon_state, STATE_RECORDING)

    def test_partial_and_final_signals_dispatch(self) -> None:
        self.bridge._on_name_appeared(self.connection, "name", "owner")
        self.controller.enable()
        self.controller.focus_in()

        callback = self.connection.subscriptions[1][6]
        callback(
            self.connection,
            "sender",
            "/io/github/pizzimenti/WhisperDictate1",
            "io.github.pizzimenti.WhisperDictate1",
            "PartialTranscript",
            FakeVariant(("hello world",)),
            None,
        )
        callback(
            self.connection,
            "sender",
            "/io/github/pizzimenti/WhisperDictate1",
            "io.github.pizzimenti.WhisperDictate1",
            "FinalTranscript",
            FakeVariant(("hello world",)),
            None,
        )

        self.assertIn(("commit_text", "hello world"), self.adapter.actions)

    def test_name_vanished_clears_availability(self) -> None:
        self.bridge._on_name_appeared(self.connection, "name", "owner")
        self.controller.enable()
        self.controller.focus_in()
        self.bridge._on_name_vanished(self.connection, "name")

        self.assertFalse(self.controller.state.daemon_available)
        self.assertIn(("update_preedit", "", False, "clear"), self.adapter.actions)

    def test_bridge_start_and_stop_manage_watch_id(self) -> None:
        self.bridge.start()
        self.assertEqual(self.bridge._watch_id, 42)
        self.bridge.stop()
        self.assertIsNone(self.bridge._watch_id)


class DaemonControlBridgeTest(unittest.TestCase):
    def test_toggle_invokes_session_bus_method(self) -> None:
        logger = logging.getLogger("whisper_dictate.tests")
        connection = FakeConnection()
        bridge = DaemonControlBridge(
            logger,
            bus_get_sync=lambda *_args: connection,
        )

        bridge.toggle()

        self.assertEqual(connection.calls[0][2], "io.github.pizzimenti.WhisperDictate1")
        self.assertEqual(connection.calls[0][3], "Toggle")


if __name__ == "__main__":
    unittest.main()
