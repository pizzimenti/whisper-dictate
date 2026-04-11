"""Unit tests for the KWin accessibility-keyboard hotkey listener."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from kdictate.core.kwin_hotkey import (
    CLIENT_NAME,
    DEFAULT_HOTKEY_KEYSYM,
    KwinHotkeyListener,
    expand_modifier_masks,
)


class _FakeVariant:
    """Carry the unpacked tuple through fake DBus call_sync results."""

    def __init__(self, payload: tuple) -> None:
        self._payload = payload

    def unpack(self) -> tuple:
        return self._payload


class _FakeConnection:
    """Record every DBus call/subscription so the test can assert on them."""

    def __init__(
        self,
        request_name_reply: int = 1,
        fail_methods: tuple[str, ...] = (),
    ) -> None:
        self.calls: list[dict] = []
        self.subscriptions: list[dict] = []
        self.unsubscribed: list[int] = []
        self._request_name_reply = request_name_reply
        self._fail_methods = set(fail_methods)
        self._next_sub_id = 100
        self.signal_callback: Any = None

    def call_sync(
        self,
        bus_name: str,
        object_path: str,
        interface_name: str,
        method: str,
        parameters: Any,
        reply_type: Any,
        flags: Any,
        timeout_ms: int,
        cancellable: Any,
    ) -> _FakeVariant:
        del reply_type, flags, timeout_ms, cancellable
        self.calls.append(
            {
                "bus_name": bus_name,
                "object_path": object_path,
                "interface": interface_name,
                "method": method,
                "parameters": parameters,
            }
        )
        if method in self._fail_methods:
            raise RuntimeError(f"fake D-Bus failure on {method}")
        if method == "RequestName":
            return _FakeVariant((self._request_name_reply,))
        return _FakeVariant(())

    def signal_subscribe(
        self,
        bus_name: str,
        interface_name: str,
        signal_name: str,
        object_path: str,
        arg0: Any,
        flags: Any,
        callback: Any,
    ) -> int:
        del arg0, flags
        sub_id = self._next_sub_id
        self._next_sub_id += 1
        self.subscriptions.append(
            {
                "id": sub_id,
                "bus_name": bus_name,
                "interface": interface_name,
                "signal": signal_name,
                "object_path": object_path,
            }
        )
        self.signal_callback = callback
        return sub_id

    def signal_unsubscribe(self, sub_id: int) -> None:
        self.unsubscribed.append(sub_id)


def _fake_gi() -> tuple[Any, Any]:
    """Stub Gio/GLib namespaces with the bits the listener actually touches."""

    Gio = SimpleNamespace(
        DBusCallFlags=SimpleNamespace(NONE=0),
        DBusSignalFlags=SimpleNamespace(NONE=0),
        BusType=SimpleNamespace(SESSION=1),
        bus_get_sync=lambda *args, **kwargs: None,
    )
    GLib = SimpleNamespace(
        Variant=lambda signature, payload: ("variant", signature, payload),
        VariantType=lambda signature: ("variant_type", signature),
    )
    return Gio, GLib


class ExpandModifierMasksTest(unittest.TestCase):
    def test_no_ignored_bits_returns_required_only(self) -> None:
        self.assertEqual(expand_modifier_masks(0x04, 0x00), [0x04])

    def test_capslock_bit_doubles_masks(self) -> None:
        # Required Ctrl (0x04), ignored CapsLock (0x02) →
        # {0x04, 0x06}.
        self.assertEqual(expand_modifier_masks(0x04, 0x02), [0x04, 0x06])

    def test_capslock_and_numlock_quadruples_masks(self) -> None:
        # Required Ctrl (0x04), ignored CapsLock+NumLock (0x12) →
        # {0x04, 0x06, 0x14, 0x16}.
        self.assertEqual(
            expand_modifier_masks(0x04, 0x12),
            [0x04, 0x06, 0x14, 0x16],
        )


class KwinHotkeyListenerStartTest(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = _FakeConnection(request_name_reply=1)
        self.callback = MagicMock()
        self.listener = KwinHotkeyListener(
            on_release=self.callback,
            connection=self.connection,
        )
        self.listener._load_gi = lambda: _fake_gi()  # type: ignore[assignment]

    def test_start_owns_name_subscribes_and_installs_grabs(self) -> None:
        self.listener.start()

        request_name_calls = [c for c in self.connection.calls if c["method"] == "RequestName"]
        set_grab_calls = [c for c in self.connection.calls if c["method"] == "SetKeyGrabs"]
        self.assertEqual(len(request_name_calls), 1)
        self.assertEqual(len(set_grab_calls), 1)

        # SetKeyGrabs payload should carry one (keysym, mask) tuple per
        # mask permutation, all using XK_space.
        _, _, payload = set_grab_calls[0]["parameters"]
        _, keystrokes = payload
        self.assertEqual(
            sorted(keystrokes),
            sorted([(DEFAULT_HOTKEY_KEYSYM, mask) for mask in self.listener.masks]),
        )

        self.assertEqual(len(self.connection.subscriptions), 1)
        sub = self.connection.subscriptions[0]
        self.assertEqual(sub["interface"], "org.freedesktop.a11y.KeyboardMonitor")
        self.assertEqual(sub["signal"], "KeyEvent")

    def test_request_name_failure_raises(self) -> None:
        self.connection = _FakeConnection(request_name_reply=3)  # 3 = EXISTS
        self.listener = KwinHotkeyListener(
            on_release=self.callback,
            connection=self.connection,
        )
        self.listener._load_gi = lambda: _fake_gi()  # type: ignore[assignment]

        with self.assertRaises(RuntimeError) as ctx:
            self.listener.start()
        self.assertIn(CLIENT_NAME, str(ctx.exception))


class _ManualClock:
    """Controllable monotonic clock for press-dedupe tests."""

    def __init__(self) -> None:
        self.now = 1000.0  # arbitrary non-zero start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class KwinHotkeyListenerKeyEventTest(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = _FakeConnection(request_name_reply=1)
        self.callback = MagicMock()
        self.clock = _ManualClock()
        self.listener = KwinHotkeyListener(
            on_release=self.callback,
            connection=self.connection,
            clock=self.clock,
        )
        self.listener._load_gi = lambda: _fake_gi()  # type: ignore[assignment]
        self.listener.start()
        self._fire = self.connection.signal_callback

    def _send(self, released: bool, keysym: int = DEFAULT_HOTKEY_KEYSYM) -> None:
        # KWin payload: (released, state, keysym, unichar, keycode)
        self._fire(None, "", "", "", "", _FakeVariant((released, 0x04, keysym, 0, 65)))

    def test_press_invokes_callback_once(self) -> None:
        self._send(released=False)
        self.callback.assert_called_once_with()

    def test_release_event_alone_does_not_invoke_callback(self) -> None:
        # Releases are intentionally ignored — kwin only delivers a release
        # KeyEvent when the mask still matches the grab, so a release-driven
        # state machine would be permanently stuck if the user lifts Ctrl
        # before Space.
        self._send(released=True)
        self.callback.assert_not_called()

    def test_duplicate_press_fan_out_only_counts_once(self) -> None:
        # KWin emits one KeyEvent per registered modifier mask permutation,
        # so a single physical press fans out into ~4 events that arrive
        # within microseconds. The dedupe window collapses them.
        for _ in range(4):
            self._send(released=False)
        self.callback.assert_called_once_with()

    def test_press_after_dedupe_window_fires_again(self) -> None:
        # Two physical presses with a real gap between them must each fire.
        self._send(released=False)
        self.clock.advance(0.100)  # 100ms — well outside the 20ms window
        self._send(released=False)
        self.assertEqual(self.callback.call_count, 2)

    def test_ctrl_first_release_does_not_lock_state(self) -> None:
        # Regression test for the P2 codex finding (kwin_hotkey.py:264).
        #
        # Sequence: press Ctrl+Space → release Ctrl first (kwin drops the
        # later Space release because the mask no longer matches our grab)
        # → press Ctrl+Space again. The previous release-driven state
        # machine left _key_held=True after the first activation and
        # silently dropped every subsequent press until a "clean" release
        # happened to land. The press-driven implementation must fire on
        # both physical presses.
        self._send(released=False)              # first press
        # NOTE: no release event arrives here — that's the bug condition
        self.clock.advance(0.250)               # user reaches for the key again
        self._send(released=False)              # second press
        self.assertEqual(self.callback.call_count, 2)

    def test_press_followed_by_release_still_only_fires_once(self) -> None:
        # The "happy path" sequence (release order Space-then-Ctrl, so
        # kwin does deliver the release event) must not double-fire.
        self._send(released=False)
        self._send(released=True)
        self.callback.assert_called_once_with()

    def test_other_keysyms_are_ignored(self) -> None:
        self._send(released=False, keysym=0x61)  # 'a'
        self._send(released=True, keysym=0x61)
        self.callback.assert_not_called()

    def test_callback_exceptions_do_not_propagate(self) -> None:
        self.callback.side_effect = RuntimeError("boom")
        # Should not raise:
        self._send(released=False)
        self.callback.assert_called_once_with()


class KwinHotkeyListenerStopTest(unittest.TestCase):
    def test_stop_unsubscribes_clears_grabs_and_releases_name(self) -> None:
        connection = _FakeConnection(request_name_reply=1)
        listener = KwinHotkeyListener(
            on_release=lambda: None,
            connection=connection,
        )
        listener._load_gi = lambda: _fake_gi()  # type: ignore[assignment]
        listener.start()
        sub_id = connection.subscriptions[0]["id"]
        connection.calls.clear()

        listener.stop()

        self.assertEqual(connection.unsubscribed, [sub_id])
        methods = [c["method"] for c in connection.calls]
        self.assertIn("SetKeyGrabs", methods)
        self.assertIn("ReleaseName", methods)

    def test_stop_after_partial_start_releases_orca_name(self) -> None:
        # RequestName succeeds (so the Orca name is squatted), but
        # SetKeyGrabs raises — e.g. we are not on a KWin session.
        # The caller is expected to invoke stop() to unwind, and
        # stop() must release the Orca name so it doesn't leak for
        # the rest of the daemon's lifetime.
        connection = _FakeConnection(
            request_name_reply=1,
            fail_methods=("SetKeyGrabs",),
        )
        listener = KwinHotkeyListener(
            on_release=lambda: None,
            connection=connection,
        )
        listener._load_gi = lambda: _fake_gi()  # type: ignore[assignment]

        with self.assertRaises(RuntimeError):
            listener.start()
        # Sanity: the partial start did claim the Orca name.
        self.assertTrue(listener._owns_name)

        connection.calls.clear()
        listener.stop()

        methods = [c["method"] for c in connection.calls]
        self.assertIn("ReleaseName", methods)
        self.assertFalse(listener._owns_name)

    def test_stop_after_request_name_failure_is_quiet_noop(self) -> None:
        # RequestName itself raises — start() never claimed anything
        # and stop() must NOT call SetKeyGrabs or ReleaseName, because
        # both would log noisy spurious warnings about state we never
        # held.
        connection = _FakeConnection(
            request_name_reply=1,
            fail_methods=("RequestName",),
        )
        listener = KwinHotkeyListener(
            on_release=lambda: None,
            connection=connection,
        )
        listener._load_gi = lambda: _fake_gi()  # type: ignore[assignment]

        with self.assertRaises(RuntimeError):
            listener.start()
        self.assertFalse(listener._owns_name)

        connection.calls.clear()
        listener.stop()

        methods = [c["method"] for c in connection.calls]
        self.assertNotIn("SetKeyGrabs", methods)
        self.assertNotIn("ReleaseName", methods)


if __name__ == "__main__":
    unittest.main()
