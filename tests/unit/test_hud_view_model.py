"""Unit tests for HUD view-model presentation mapping."""

from __future__ import annotations

import unittest

from kdictate.hud.state import HudModel, HudPhase
from kdictate.hud.view_model import MAX_DISPLAY_LENGTH, HudPresentation, present


class PresentHiddenTest(unittest.TestCase):
    def test_hidden_is_invisible(self) -> None:
        p = present(HudModel(phase=HudPhase.HIDDEN))
        self.assertFalse(p.visible)
        self.assertEqual(p.label, "")


class PresentStartingTest(unittest.TestCase):
    def test_starting_shows_label(self) -> None:
        p = present(HudModel(phase=HudPhase.STARTING))
        self.assertTrue(p.visible)
        self.assertIn("Starting", p.label)
        self.assertEqual(p.style, "neutral")


class PresentListeningTest(unittest.TestCase):
    def test_listening_shows_label(self) -> None:
        p = present(HudModel(phase=HudPhase.LISTENING))
        self.assertTrue(p.visible)
        self.assertIn("Listening", p.label)
        self.assertEqual(p.style, "active")


class PresentPartialTest(unittest.TestCase):
    def test_partial_shows_text(self) -> None:
        p = present(HudModel(phase=HudPhase.PARTIAL, partial_text="hello world"))
        self.assertTrue(p.visible)
        self.assertEqual(p.label, "hello world")
        self.assertEqual(p.style, "active")

    def test_long_partial_is_truncated(self) -> None:
        long_text = "x" * (MAX_DISPLAY_LENGTH + 20)
        p = present(HudModel(phase=HudPhase.PARTIAL, partial_text=long_text))
        self.assertTrue(len(p.label) <= MAX_DISPLAY_LENGTH)
        self.assertTrue(p.label.endswith("..."))


class PresentTranscribingTest(unittest.TestCase):
    def test_transcribing_shows_label(self) -> None:
        p = present(HudModel(phase=HudPhase.TRANSCRIBING))
        self.assertTrue(p.visible)
        self.assertIn("Transcribing", p.label)
        self.assertEqual(p.style, "neutral")


class PresentCommittingTest(unittest.TestCase):
    def test_committing_shows_committed_text(self) -> None:
        p = present(HudModel(phase=HudPhase.COMMITTING, commit_text="hello world"))
        self.assertTrue(p.visible)
        self.assertIn("hello world", p.label)
        self.assertIn("Committed", p.label)
        self.assertEqual(p.style, "success")

    def test_long_commit_is_truncated(self) -> None:
        long_text = "y" * (MAX_DISPLAY_LENGTH + 20)
        p = present(HudModel(phase=HudPhase.COMMITTING, commit_text=long_text))
        self.assertTrue(p.visible)
        self.assertIn("...", p.label)


class PresentErrorTest(unittest.TestCase):
    def test_error_shows_message(self) -> None:
        p = present(HudModel(phase=HudPhase.ERROR, error_message="Mic not found"))
        self.assertTrue(p.visible)
        self.assertEqual(p.label, "Mic not found")
        self.assertEqual(p.style, "error")

    def test_error_without_message_shows_fallback(self) -> None:
        p = present(HudModel(phase=HudPhase.ERROR, error_message=""))
        self.assertTrue(p.visible)
        self.assertIn("Unknown error", p.label)


class PresentReturnTypeTest(unittest.TestCase):
    def test_all_phases_return_presentation(self) -> None:
        for phase in HudPhase:
            p = present(HudModel(phase=phase))
            self.assertIsInstance(p, HudPresentation)
            self.assertIn(p.style, {"neutral", "active", "success", "error"})


if __name__ == "__main__":
    unittest.main()
