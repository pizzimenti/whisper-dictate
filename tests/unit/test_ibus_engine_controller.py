from __future__ import annotations

import logging
import unittest

from kdictate.constants import STATE_ERROR, STATE_IDLE, STATE_RECORDING, STATE_TRANSCRIBING
from kdictate.ibus_engine.controller import DictationEngineController, PreeditPresentation


class FakeAdapter:
    def __init__(self) -> None:
        self.actions: list[tuple] = []
        self.visible: bool = False
        self.last_partial: str = ""
        self.last_mode: str = "idle"

    def set_preedit(self, presentation: PreeditPresentation | None) -> None:
        if presentation is None:
            self.actions.append(("hide",))
            self.visible = False
            self.last_partial = ""
            self.last_mode = "idle"
            return
        self.actions.append(("show", presentation.partial, presentation.mode))
        self.visible = True
        self.last_partial = presentation.partial
        self.last_mode = presentation.mode

    def commit_text(self, text: str) -> None:
        self.actions.append(("commit", text))


class DictationEngineControllerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = FakeAdapter()
        self.logger = logging.getLogger("kdictate.tests")
        self.controller = DictationEngineController(self.adapter, self.logger)

    def _ready_recording(self) -> None:
        self.controller.set_daemon_available(True)
        self.controller.enable()
        self.controller.focus_in()
        self.controller.handle_state_changed(STATE_RECORDING)

    def test_partial_transcript_renders_preedit_when_focused(self) -> None:
        self._ready_recording()

        self.controller.handle_partial_transcript("hello   world\n")

        self.assertEqual(self.adapter.last_partial, "hello world")
        self.assertEqual(self.adapter.last_mode, "listening")
        self.assertTrue(self.controller.state.preedit_visible)
        self.assertEqual(self.controller.state.pending_partial, "hello world")

    def test_final_transcript_clears_preedit_then_commits_when_focused(self) -> None:
        self._ready_recording()
        self.controller.handle_partial_transcript("hello world")
        self.adapter.actions.clear()

        self.controller.handle_final_transcript("hello world")

        self.assertEqual(
            self.adapter.actions,
            [("hide",), ("commit", "hello world")],
        )
        self.assertFalse(self.controller.state.preedit_visible)
        self.assertEqual(self.controller.state.last_final, "hello world")

    def test_final_transcript_without_focus_is_dropped_and_clears_preedit(self) -> None:
        self._ready_recording()
        self.controller.handle_partial_transcript("hello world")
        self.controller.focus_out()
        self.adapter.actions.clear()

        self.controller.handle_final_transcript("hello world")

        self.assertEqual(self.adapter.actions, [("hide",)])
        self.assertFalse(any(a[0] == "commit" for a in self.adapter.actions))

    def test_daemon_disconnect_and_reconnect_reconcile_state(self) -> None:
        self._ready_recording()
        self.controller.handle_partial_transcript("hello")

        self.controller.set_daemon_available(False)
        self.controller.set_daemon_available(True)
        self.controller.handle_state_changed(STATE_RECORDING)
        self.controller.handle_partial_transcript("world")

        # After reconnect we should see "world" as the visible state.
        self.assertEqual(self.adapter.last_partial, "world")
        self.assertEqual(self.adapter.last_mode, "listening")

    def test_invalid_state_is_normalized_to_error(self) -> None:
        self._ready_recording()
        self.controller.handle_partial_transcript("stale text")

        self.controller.handle_state_changed("bogus")

        self.assertEqual(self.controller.state.daemon_state, STATE_ERROR)
        self.assertEqual(self.controller.state.pending_partial, "")
        self.assertIn(("hide",), self.adapter.actions)

    def test_final_transcript_commits_after_state_transitions_to_idle(self) -> None:
        self._ready_recording()
        self.controller.handle_partial_transcript("hello world")

        self.controller.handle_state_changed(STATE_IDLE)
        self.controller.handle_final_transcript("hello world")

        self.assertIn(("commit", "hello world"), self.adapter.actions)

    def test_focus_out_clears_preedit(self) -> None:
        self._ready_recording()
        self.controller.handle_partial_transcript("hello world")

        self.controller.focus_out()

        self.assertFalse(self.controller.state.focused)
        self.assertFalse(self.adapter.visible)

    def test_transcribing_animation_starts_when_safe(self) -> None:
        self._ready_recording()
        self.adapter.actions.clear()

        self.controller.handle_state_changed(STATE_TRANSCRIBING)

        self.assertEqual(self.adapter.last_mode, "transcribing")
        self.assertTrue(self.adapter.visible)

    def test_transcribing_animation_skipped_without_focus(self) -> None:
        self.controller.set_daemon_available(True)
        self.controller.enable()
        # Note: no focus_in()

        self.controller.handle_state_changed(STATE_TRANSCRIBING)

        self.assertFalse(self.adapter.visible)

    def test_transcribing_animation_skipped_when_disabled(self) -> None:
        self.controller.set_daemon_available(True)
        self.controller.focus_in()
        # Note: no enable()

        self.controller.handle_state_changed(STATE_TRANSCRIBING)

        self.assertFalse(self.adapter.visible)

    def test_focus_out_stops_animation(self) -> None:
        self._ready_recording()

        self.controller.focus_out()

        self.assertFalse(self.adapter.visible)

    def test_disable_stops_animation(self) -> None:
        self._ready_recording()

        self.controller.disable()

        self.assertFalse(self.adapter.visible)

    def test_daemon_lost_stops_animation(self) -> None:
        self._ready_recording()

        self.controller.set_daemon_available(False)

        self.assertFalse(self.adapter.visible)

    def test_partial_preserved_through_transcribing(self) -> None:
        self._ready_recording()
        self.controller.handle_partial_transcript("hello world")

        self.controller.handle_state_changed(STATE_TRANSCRIBING)

        # The pending partial must remain so it can survive the transition
        self.assertEqual(self.controller.state.pending_partial, "hello world")
        # Animation started in transcribing mode with the partial preserved
        self.assertEqual(self.adapter.last_partial, "hello world")
        self.assertEqual(self.adapter.last_mode, "transcribing")

    def test_recording_with_no_partial_shows_listening(self) -> None:
        self.controller.set_daemon_available(True)
        self.controller.enable()
        self.controller.focus_in()

        self.controller.handle_state_changed(STATE_RECORDING)

        self.assertEqual(self.adapter.last_mode, "listening")
        self.assertEqual(self.adapter.last_partial, "")


if __name__ == "__main__":
    unittest.main()
