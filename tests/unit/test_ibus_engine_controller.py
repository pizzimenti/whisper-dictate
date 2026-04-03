from __future__ import annotations

import logging
import unittest

from whisper_dictate.constants import STATE_ERROR, STATE_IDLE, STATE_RECORDING
from whisper_dictate.ibus_engine.controller import DictationEngineController


class FakeAdapter:
    def __init__(self) -> None:
        self.actions: list[tuple[str, object]] = []

    def update_preedit(self, text: str, *, visible: bool, focus_mode: str) -> None:
        self.actions.append(("update_preedit", text, visible, focus_mode))

    def commit_text(self, text: str) -> None:
        self.actions.append(("commit_text", text))


class DictationEngineControllerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = FakeAdapter()
        self.logger = logging.getLogger("whisper_dictate.tests")
        self.controller = DictationEngineController(self.adapter, self.logger)

    def test_partial_transcript_renders_preedit_when_focused(self) -> None:
        self.controller.set_daemon_available(True)
        self.controller.handle_state_changed(STATE_RECORDING)
        self.controller.enable()
        self.controller.focus_in()

        self.controller.handle_partial_transcript("hello   world\n")

        self.assertEqual(
            self.adapter.actions,
            [("update_preedit", "hello world", True, "clear")],
        )
        self.assertTrue(self.controller.state.preedit_visible)
        self.assertEqual(self.controller.state.pending_partial, "hello world")

    def test_final_transcript_clears_preedit_then_commits_when_focused(self) -> None:
        self.controller.set_daemon_available(True)
        self.controller.handle_state_changed(STATE_RECORDING)
        self.controller.enable()
        self.controller.focus_in()
        self.controller.handle_partial_transcript("hello world")

        self.controller.handle_final_transcript("hello world")

        self.assertEqual(
            self.adapter.actions,
            [
                ("update_preedit", "hello world", True, "clear"),
                ("update_preedit", "", False, "clear"),
                ("commit_text", "hello world"),
            ],
        )
        self.assertFalse(self.controller.state.preedit_visible)
        self.assertEqual(self.controller.state.last_final, "hello world")

    def test_final_transcript_without_focus_is_dropped_and_clears_preedit(self) -> None:
        self.controller.set_daemon_available(True)
        self.controller.handle_state_changed(STATE_RECORDING)
        self.controller.enable()
        self.controller.focus_in()
        self.controller.handle_partial_transcript("hello world")
        self.controller.focus_out()

        self.controller.handle_final_transcript("hello world")

        self.assertEqual(
            self.adapter.actions,
            [
                ("update_preedit", "hello world", True, "clear"),
                ("update_preedit", "", False, "clear"),
                ("update_preedit", "", False, "clear"),
            ],
        )
        self.assertEqual(len([a for a in self.adapter.actions if a[0] == "commit_text"]), 0)

    def test_daemon_disconnect_and_reconnect_reconcile_state(self) -> None:
        self.controller.set_daemon_available(True)
        self.controller.handle_state_changed(STATE_RECORDING)
        self.controller.enable()
        self.controller.focus_in()
        self.controller.handle_partial_transcript("hello")

        self.controller.set_daemon_available(False)
        self.controller.set_daemon_available(True)
        self.controller.handle_state_changed(STATE_RECORDING)
        self.controller.handle_partial_transcript("world")

        self.assertEqual(
            self.adapter.actions,
            [
                ("update_preedit", "hello", True, "clear"),
                ("update_preedit", "", False, "clear"),
                ("update_preedit", "world", True, "clear"),
            ],
        )

    def test_invalid_state_is_normalized_to_error(self) -> None:
        self.controller.handle_state_changed("bogus")

        self.assertEqual(self.controller.state.daemon_state, STATE_ERROR)
        self.assertIn(("update_preedit", "", False, "clear"), self.adapter.actions)

    def test_final_transcript_commits_after_state_transitions_to_idle(self) -> None:
        self.controller.set_daemon_available(True)
        self.controller.handle_state_changed(STATE_RECORDING)
        self.controller.enable()
        self.controller.focus_in()
        self.controller.handle_partial_transcript("hello world")

        self.controller.handle_state_changed(STATE_IDLE)
        self.controller.handle_final_transcript("hello world")

        self.assertIn(("commit_text", "hello world"), self.adapter.actions)

    def test_focus_out_clears_preedit(self) -> None:
        self.controller.set_daemon_available(True)
        self.controller.handle_state_changed(STATE_RECORDING)
        self.controller.enable()
        self.controller.focus_in()
        self.controller.handle_partial_transcript("hello world")

        self.controller.focus_out()

        self.assertFalse(self.controller.state.focused)
        self.assertIn(("update_preedit", "", False, "clear"), self.adapter.actions)


if __name__ == "__main__":
    unittest.main()
