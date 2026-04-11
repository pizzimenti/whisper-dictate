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

    def __init__(self, request_name_reply: int = 1) -> None:
        self.calls: list[dict] = []
        self.subscriptions: list[dict] = []
        self.unsubscribed: list[int] = []
        self._request_name_reply = request_name_reply
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


class KwinHotkeyListenerKeyEventTest(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = _FakeConnection(request_name_reply=1)
        self.callback = MagicMock()
        self.listener = KwinHotkeyListener(
            on_release=self.callback,
            connection=self.connection,
        )
        self.listener._load_gi = lambda: _fake_gi()  # type: ignore[assignment]
        self.listener.start()
        self._fire = self.connection.signal_callback

    def _send(self, released: bool, keysym: int = DEFAULT_HOTKEY_KEYSYM) -> None:
        # KWin payload: (released, state, keysym, unichar, keycode)
        self._fire(None, "", "", "", "", _FakeVariant((released, 0x04, keysym, 0, 65)))

    def test_release_after_press_invokes_callback_once(self) -> None:
        self._send(released=False)
        self._send(released=True)
        self.callback.assert_called_once_with()

    def test_duplicate_press_events_only_count_once(self) -> None:
        # KWin emits one KeyEvent per registered modifier mask, so a single
        # physical press can fire 4× before the matching release.
        for _ in range(4):
            self._send(released=False)
        self._send(released=True)
        self.callback.assert_called_once_with()

    def test_release_without_prior_press_does_nothing(self) -> None:
        self._send(released=True)
        self.callback.assert_not_called()

    def test_other_keysyms_are_ignored(self) -> None:
        self._send(released=False, keysym=0x61)  # 'a'
        self._send(released=True, keysym=0x61)
        self.callback.assert_not_called()

    def test_callback_exceptions_do_not_propagate(self) -> None:
        self.callback.side_effect = RuntimeError("boom")
        self._send(released=False)
        # Should not raise:
        self._send(released=True)
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


if __name__ == "__main__":
    unittest.main()
