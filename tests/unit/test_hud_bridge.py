"""Direct unit tests for the HUD D-Bus bridge."""

from __future__ import annotations

import logging
import unittest
from dataclasses import dataclass, field
from typing import Any


class FakeVariant:
    def __init__(self, values: tuple[object, ...]) -> None:
        self._values = values

    def unpack(self) -> tuple[object, ...]:
        return self._values


class BrokenVariant:
    def unpack(self) -> tuple[object, ...]:
        raise RuntimeError("corrupt payload")


class _FakeAsyncResult:
    def __init__(self, result: object) -> None:
        self.result = result


class FakeConnection:
    def __init__(self, *, seed_snapshot: tuple[str, ...] = ("idle", "", "", "", "")) -> None:
        self.subscriptions: list[tuple] = []
        self.unsubscribed: list[int] = []
        self.calls: list[tuple] = []
        self._next_id = 1
        self._seed_snapshot = seed_snapshot
        self._seed_should_fail = False

    def signal_subscribe(self, *args: Any) -> int:
        self.subscriptions.append(args)
        current = self._next_id
        self._next_id += 1
        return current

    def signal_unsubscribe(self, subscription_id: int) -> None:
        self.unsubscribed.append(subscription_id)

    def call(self, *args: Any) -> None:
        self.calls.append(args[:9])
        callback = args[9] if len(args) > 9 else None
        user_data = args[10] if len(args) > 10 else None
        if callback is not None:
            if self._seed_should_fail:
                callback(self, _FakeAsyncResult(None), user_data)
            else:
                callback(
                    self,
                    _FakeAsyncResult(FakeVariant(self._seed_snapshot)),
                    user_data,
                )

    def call_finish(self, async_result: _FakeAsyncResult) -> object:
        if async_result.result is None:
            raise RuntimeError("GetSnapshot failed")
        return async_result.result


@dataclass
class CallbackLog:
    appeared: int = 0
    vanished: int = 0
    states: list[str] = field(default_factory=list)
    partials: list[str] = field(default_factory=list)
    finals: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    snapshots: list[tuple[str, ...]] = field(default_factory=list)


def _make_bridge(
    log: CallbackLog,
    seed_snapshot: tuple[str, ...] = ("idle", "", "", "", ""),
) -> tuple[Any, FakeConnection]:
    from kdictate.hud.dbus_client import HudDaemonBridge

    conn = FakeConnection(seed_snapshot=seed_snapshot)
    bridge = HudDaemonBridge(
        on_daemon_appeared=lambda: setattr(log, "appeared", log.appeared + 1),
        on_daemon_vanished=lambda: setattr(log, "vanished", log.vanished + 1),
        on_state_changed=lambda s: log.states.append(s),
        on_partial_transcript=lambda t: log.partials.append(t),
        on_final_transcript=lambda t: log.finals.append(t),
        on_error=lambda c, m: log.errors.append((c, m)),
        on_snapshot=lambda *a: log.snapshots.append(a),
        logger=logging.getLogger("kdictate.tests.hud.bridge"),
        watch_name=lambda *args: 42,
        unwatch_name=lambda _: None,
    )
    return bridge, conn


class HudDaemonBridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.log = CallbackLog()

    def test_name_appeared_subscribes_and_seeds_snapshot(self) -> None:
        bridge, conn = _make_bridge(
            self.log, seed_snapshot=("recording", "hello", "", "", ""),
        )
        bridge._on_name_appeared(conn, "name", "owner")

        self.assertEqual(self.log.appeared, 1)
        self.assertEqual(len(conn.subscriptions), 4)
        self.assertEqual(len(self.log.snapshots), 1)
        self.assertEqual(self.log.snapshots[0], ("recording", "hello", "", "", ""))

    def test_name_vanished_unsubscribes_and_notifies(self) -> None:
        bridge, conn = _make_bridge(self.log)
        bridge._on_name_appeared(conn, "name", "owner")
        bridge._on_name_vanished(conn, "name")

        self.assertEqual(self.log.vanished, 1)
        self.assertEqual(len(conn.unsubscribed), 4)

    def test_signal_dispatch_state_changed(self) -> None:
        bridge, conn = _make_bridge(self.log)
        bridge._on_name_appeared(conn, "name", "owner")
        self.log.states.clear()

        bridge._on_signal(conn, "", "", "", "StateChanged", FakeVariant(("recording",)))
        self.assertEqual(self.log.states, ["recording"])

    def test_signal_dispatch_partial(self) -> None:
        bridge, conn = _make_bridge(self.log)
        bridge._on_signal(conn, "", "", "", "PartialTranscript", FakeVariant(("hello",)))
        self.assertEqual(self.log.partials, ["hello"])

    def test_signal_dispatch_final(self) -> None:
        bridge, conn = _make_bridge(self.log)
        bridge._on_signal(conn, "", "", "", "FinalTranscript", FakeVariant(("done",)))
        self.assertEqual(self.log.finals, ["done"])

    def test_signal_dispatch_error(self) -> None:
        bridge, conn = _make_bridge(self.log)
        bridge._on_signal(conn, "", "", "", "ErrorOccurred", FakeVariant(("mic", "fail")))
        self.assertEqual(self.log.errors, [("mic", "fail")])

    def test_malformed_payload_is_ignored(self) -> None:
        bridge, conn = _make_bridge(self.log)
        bridge._on_signal(conn, "", "", "", "StateChanged", BrokenVariant())
        self.assertEqual(self.log.states, [])

    def test_wrong_arity_is_ignored(self) -> None:
        bridge, conn = _make_bridge(self.log)
        bridge._on_signal(conn, "", "", "", "StateChanged", FakeVariant(("a", "b")))
        self.assertEqual(self.log.states, [])

    def test_seed_failure_does_not_crash(self) -> None:
        bridge, conn = _make_bridge(self.log)
        conn._seed_should_fail = True
        bridge._on_name_appeared(conn, "name", "owner")
        self.assertEqual(self.log.appeared, 1)
        self.assertEqual(self.log.snapshots, [])

    def test_vanish_before_seed_reply_drops_stale_snapshot(self) -> None:
        """Seed issued, daemon vanishes, stale reply arrives."""
        log = self.log
        captured_callback: list[Any] = []

        class DelayedConnection(FakeConnection):
            def call(self, *args: Any) -> None:
                self.calls.append(args[:9])
                callback = args[9] if len(args) > 9 else None
                user_data = args[10] if len(args) > 10 else None
                if callback is not None:
                    captured_callback.append((callback, user_data))

            def call_finish(self, async_result: Any) -> Any:
                return async_result.result

        conn = DelayedConnection()
        bridge, _ = _make_bridge(log)
        # Replace connection internals with delayed version
        bridge._on_name_appeared(conn, "name", "owner")
        self.assertEqual(len(captured_callback), 1)

        bridge._on_name_vanished(conn, "name")

        cb, ud = captured_callback[0]
        cb(conn, _FakeAsyncResult(FakeVariant(("recording", "hello", "", "", ""))), ud)
        self.assertEqual(log.snapshots, [])

    def test_reappear_before_old_seed_reply_drops_stale_snapshot(self) -> None:
        """Seed issued, daemon vanishes and reappears, old reply arrives."""
        log = self.log
        captured_callbacks: list[Any] = []

        class DelayedConnection(FakeConnection):
            def call(self, *args: Any) -> None:
                self.calls.append(args[:9])
                callback = args[9] if len(args) > 9 else None
                user_data = args[10] if len(args) > 10 else None
                if callback is not None:
                    captured_callbacks.append((callback, user_data))

            def call_finish(self, async_result: Any) -> Any:
                return async_result.result

        conn = DelayedConnection()
        bridge, _ = _make_bridge(log)

        # First appear -- seed issued (generation=1)
        bridge._on_name_appeared(conn, "name", "owner")
        self.assertEqual(len(captured_callbacks), 1)

        # Vanish + reappear -- new seed issued (generation=2)
        bridge._on_name_vanished(conn, "name")
        bridge._on_name_appeared(conn, "name", "owner")
        self.assertEqual(len(captured_callbacks), 2)

        # Old reply from generation 1 -- must be dropped
        old_cb, old_ud = captured_callbacks[0]
        old_cb(conn, _FakeAsyncResult(FakeVariant(("error", "", "", "e", "boom"))), old_ud)
        self.assertEqual(log.snapshots, [])

        # New reply from generation 2 -- must be accepted
        new_cb, new_ud = captured_callbacks[1]
        new_cb(conn, _FakeAsyncResult(FakeVariant(("recording", "hi", "", "", ""))), new_ud)
        self.assertEqual(len(log.snapshots), 1)
        self.assertEqual(log.snapshots[0], ("recording", "hi", "", "", ""))

    def test_start_and_stop_manage_watch_id(self) -> None:
        bridge, _conn = _make_bridge(self.log)
        bridge.start()
        self.assertEqual(bridge._watch_id, 42)
        bridge.stop()
        self.assertIsNone(bridge._watch_id)


if __name__ == "__main__":
    unittest.main()
