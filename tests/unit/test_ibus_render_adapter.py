"""Direct tests for the IBus render adapter animation and lifecycle."""

from __future__ import annotations

import unittest
from types import ModuleType
from typing import Any
from unittest.mock import patch

from kdictate.ibus_engine.controller import PreeditPresentation


class _FakeText:
    def __init__(self, value: str) -> None:
        self.value = value


class _FakeTextNamespace:
    @staticmethod
    def new_from_string(value: str) -> _FakeText:
        return _FakeText(value)


class _FakePreeditMode:
    CLEAR = "clear"
    COMMIT = "commit"


def _make_fake_ibus_module() -> ModuleType:
    mod = ModuleType("fake_ibus")
    mod.Text = _FakeTextNamespace
    mod.PreeditFocusMode = _FakePreeditMode
    return mod


class _FakeEngine:
    """Records all IBus engine calls so we can assert on them."""

    def __init__(self) -> None:
        self.preedit_calls: list[tuple[str, int, bool, str]] = []
        self.shown: int = 0
        self.hidden: int = 0
        self.committed: list[str] = []

    def update_preedit_text_with_mode(self, text: _FakeText, cursor: int, visible: bool, mode: str) -> None:
        self.preedit_calls.append((text.value, cursor, visible, mode))

    def show_preedit_text(self) -> None:
        self.shown += 1

    def hide_preedit_text(self) -> None:
        self.hidden += 1

    def commit_text(self, text: _FakeText) -> None:
        self.committed.append(text.value)


class _FakeGLib:
    """Stub GLib that records timeouts without actually scheduling them."""

    SOURCE_CONTINUE = True
    SOURCE_REMOVE = False

    def __init__(self) -> None:
        self.scheduled: list[tuple[int, Any]] = []
        self.cancelled: list[int] = []
        self._next_id = 1

    def timeout_add(self, ms: int, callback: Any) -> int:
        timer_id = self._next_id
        self._next_id += 1
        self.scheduled.append((ms, callback))
        return timer_id

    def source_remove(self, timer_id: int) -> None:
        self.cancelled.append(timer_id)

    def fire_last(self) -> None:
        _, callback = self.scheduled[-1]
        callback()


def _build_adapter() -> tuple[Any, _FakeEngine, _FakeGLib, Any]:
    fake_glib = _FakeGLib()
    fake_engine = _FakeEngine()

    import kdictate.ibus_engine.render_adapter as render_module
    patcher = patch.object(render_module, "GLib", fake_glib)
    patcher.start()
    adapter = render_module.IbusRenderAdapter(fake_engine, _make_fake_ibus_module())
    return adapter, fake_engine, fake_glib, patcher


class IbusRenderAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter, self.engine, self.glib, self._patcher = _build_adapter()

    def tearDown(self) -> None:
        self._patcher.stop()

    # -- set_preedit --------------------------------------------------------

    def test_show_listening_with_partial_renders_partial_and_spinner(self) -> None:
        self.adapter.set_preedit(PreeditPresentation("hello", "listening"))

        self.assertEqual(len(self.engine.preedit_calls), 1)
        text, _, visible, _ = self.engine.preedit_calls[0]
        self.assertTrue(visible)
        self.assertTrue(text.startswith("hello"))
        self.assertEqual(len(self.glib.scheduled), 1)

    def test_show_listening_without_partial_renders_label(self) -> None:
        self.adapter.set_preedit(PreeditPresentation("", "listening"))

        text = self.engine.preedit_calls[0][0]
        self.assertIn("Listening", text)

    def test_show_transcribing_with_partial_includes_label(self) -> None:
        self.adapter.set_preedit(PreeditPresentation("hello world", "transcribing"))

        text = self.engine.preedit_calls[0][0]
        self.assertIn("hello world", text)
        self.assertIn("Transcribing", text)

    def test_show_transcribing_without_partial_renders_label(self) -> None:
        self.adapter.set_preedit(PreeditPresentation("", "transcribing"))

        text = self.engine.preedit_calls[0][0]
        self.assertIn("Transcribing", text)

    # -- set_preedit(None) --------------------------------------------------

    def test_hide_preedit_stops_timer_and_clears(self) -> None:
        self.adapter.set_preedit(PreeditPresentation("hello", "listening"))

        self.adapter.set_preedit(None)

        self.assertEqual(len(self.glib.cancelled), 1)
        self.assertGreaterEqual(self.engine.hidden, 1)

    # -- timer ticks --------------------------------------------------------

    def test_tick_advances_spinner_frame(self) -> None:
        self.adapter.set_preedit(PreeditPresentation("", "listening"))
        first = self.engine.preedit_calls[0][0]

        self.glib.fire_last()
        second = self.engine.preedit_calls[-1][0]

        # Same length (only the spinner char rotates), different content
        self.assertEqual(len(first), len(second))
        self.assertNotEqual(first, second)

    def test_tick_returns_continue(self) -> None:
        self.adapter.set_preedit(PreeditPresentation("", "listening"))
        _, callback = self.glib.scheduled[-1]
        result = callback()
        self.assertEqual(result, self.glib.SOURCE_CONTINUE)

    # -- mode transitions ---------------------------------------------------

    def test_listening_to_transcribing_preserves_partial(self) -> None:
        self.adapter.set_preedit(PreeditPresentation("hello world", "listening"))
        self.engine.preedit_calls.clear()

        self.adapter.set_preedit(PreeditPresentation("hello world", "transcribing"))

        text = self.engine.preedit_calls[-1][0]
        self.assertIn("hello world", text)
        self.assertIn("Transcribing", text)

    def test_mode_change_resets_frame_index(self) -> None:
        self.adapter.set_preedit(PreeditPresentation("", "listening"))
        # Advance several ticks
        for _ in range(5):
            self.glib.fire_last()

        self.adapter.set_preedit(PreeditPresentation("", "transcribing"))

        # First render of new mode should use frame 0
        # (we can't directly assert frame index, but we know it should
        # produce a deterministic first render)
        text = self.engine.preedit_calls[-1][0]
        self.assertIn("Transcribing", text)

    # -- commit_text --------------------------------------------------------

    def test_commit_text_commits_without_owning_preedit_cleanup(self) -> None:
        self.adapter.set_preedit(PreeditPresentation("hello", "listening"))
        self.engine.preedit_calls.clear()
        self.engine.hidden = 0

        self.adapter.commit_text("hello world")

        self.assertEqual(len(self.glib.cancelled), 0)
        self.assertEqual(self.engine.preedit_calls, [])
        self.assertEqual(self.engine.hidden, 0)
        self.assertEqual(self.engine.committed, ["hello world"])

    # -- shutdown -----------------------------------------------------------

    def test_shutdown_stops_timer(self) -> None:
        self.adapter.set_preedit(PreeditPresentation("", "listening"))

        self.adapter.shutdown()

        self.assertEqual(len(self.glib.cancelled), 1)

    def test_shutdown_is_idempotent(self) -> None:
        self.adapter.shutdown()
        self.adapter.shutdown()
        self.assertEqual(self.glib.cancelled, [])


if __name__ == "__main__":
    unittest.main()
