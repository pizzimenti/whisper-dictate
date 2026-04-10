"""Smoke tests for the HUD controller wiring path."""

from __future__ import annotations

import logging
import unittest
from dataclasses import dataclass, field

from kdictate.constants import STATE_IDLE, STATE_RECORDING, STATE_STARTING, STATE_TRANSCRIBING
from kdictate.hud.controller import HudController
from kdictate.hud.state import (
    CommitAckExpired,
    DaemonAppeared,
    DaemonStateChanged,
    DaemonVanished,
    ErrorOccurred,
    FinalTranscript,
    HudPhase,
    PartialTranscript,
)
from kdictate.hud.view_model import HudPresentation


@dataclass
class FakeSurface:
    """Capture presentations the controller sends to the window."""

    updates: list[HudPresentation] = field(default_factory=list)

    def update_presentation(self, presentation: HudPresentation) -> None:
        self.updates.append(presentation)

    @property
    def last(self) -> HudPresentation:
        return self.updates[-1]


@dataclass
class FakeTimer:
    """Record schedule/cancel calls without a real event loop."""

    scheduled: list[tuple[int, callable]] = field(default_factory=list)
    cancelled: list[int] = field(default_factory=list)
    _next_id: int = 1

    def schedule(self, delay_ms: int, callback: callable) -> int:
        timer_id = self._next_id
        self._next_id += 1
        self.scheduled.append((delay_ms, callback))
        return timer_id

    def cancel(self, timer_id: int) -> None:
        self.cancelled.append(timer_id)

    def fire_last(self) -> None:
        """Simulate the most recently scheduled timer firing."""
        _, callback = self.scheduled[-1]
        callback()


class HudControllerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.surface = FakeSurface()
        self.timer = FakeTimer()
        self.logger = logging.getLogger("kdictate.tests.hud")
        self.ctrl = HudController(
            window=self.surface, timer=self.timer, logger=self.logger,
        )

    def test_daemon_appeared_alone_updates_but_stays_hidden(self) -> None:
        self.ctrl.dispatch(DaemonAppeared())
        self.assertEqual(len(self.surface.updates), 1)
        self.assertFalse(self.surface.last.visible)

    def test_full_dictation_cycle_renders_expected_presentations(self) -> None:
        self.ctrl.dispatch(DaemonAppeared())
        self.ctrl.dispatch(DaemonStateChanged(STATE_STARTING))
        self.assertTrue(self.surface.last.visible)
        self.assertEqual(self.surface.last.style, "neutral")

        self.ctrl.dispatch(DaemonStateChanged(STATE_RECORDING))
        self.assertTrue(self.surface.last.visible)
        self.assertEqual(self.surface.last.style, "active")

        self.ctrl.dispatch(PartialTranscript("hello"))
        self.assertIn("hello", self.surface.last.label)
        self.assertEqual(self.surface.last.style, "active")

        self.ctrl.dispatch(DaemonStateChanged(STATE_TRANSCRIBING))
        self.assertTrue(self.surface.last.visible)
        self.assertEqual(self.surface.last.style, "neutral")

        self.ctrl.dispatch(FinalTranscript("hello world"))
        self.assertIn("hello world", self.surface.last.label)
        self.assertEqual(self.surface.last.style, "success")
        self.assertEqual(len(self.timer.scheduled), 1)

        self.ctrl.dispatch(DaemonStateChanged(STATE_IDLE))
        self.assertEqual(self.surface.last.style, "success")

        self.timer.fire_last()
        self.assertFalse(self.surface.last.visible)

    def test_error_renders_error_style(self) -> None:
        self.ctrl.dispatch(DaemonAppeared())
        self.ctrl.dispatch(DaemonStateChanged(STATE_RECORDING))
        self.ctrl.dispatch(ErrorOccurred("mic_fail", "No microphone"))

        self.assertTrue(self.surface.last.visible)
        self.assertEqual(self.surface.last.style, "error")
        self.assertIn("No microphone", self.surface.last.label)

    def test_daemon_vanished_hides_window(self) -> None:
        self.ctrl.dispatch(DaemonAppeared())
        self.ctrl.dispatch(DaemonStateChanged(STATE_RECORDING))
        self.ctrl.dispatch(PartialTranscript("hello"))
        self.assertTrue(self.surface.last.visible)

        self.ctrl.dispatch(DaemonVanished())
        self.assertFalse(self.surface.last.visible)

    def test_no_op_dispatch_does_not_update_surface(self) -> None:
        self.ctrl.dispatch(DaemonAppeared())
        count = len(self.surface.updates)
        self.ctrl.dispatch(DaemonAppeared())
        self.assertEqual(len(self.surface.updates), count)

    def test_recording_preserves_partial_during_state_reemission(self) -> None:
        self.ctrl.dispatch(DaemonAppeared())
        self.ctrl.dispatch(DaemonStateChanged(STATE_RECORDING))
        self.ctrl.dispatch(PartialTranscript("hello world"))
        self.assertIn("hello world", self.surface.last.label)

        before = len(self.surface.updates)
        self.ctrl.dispatch(DaemonStateChanged(STATE_RECORDING))
        self.assertEqual(len(self.surface.updates), before)
        self.assertEqual(self.ctrl.model.phase, HudPhase.PARTIAL)
        self.assertEqual(self.ctrl.model.partial_text, "hello world")

    def test_commit_timer_cancelled_when_new_session_starts(self) -> None:
        self.ctrl.dispatch(DaemonAppeared())
        self.ctrl.dispatch(DaemonStateChanged(STATE_RECORDING))
        self.ctrl.dispatch(FinalTranscript("done"))
        self.assertEqual(len(self.timer.scheduled), 1)

        self.ctrl.dispatch(DaemonStateChanged(STATE_STARTING))
        self.assertEqual(len(self.timer.cancelled), 1)

    def test_commit_timer_restarts_on_second_final(self) -> None:
        self.ctrl.dispatch(DaemonAppeared())
        self.ctrl.dispatch(DaemonStateChanged(STATE_RECORDING))
        self.ctrl.dispatch(FinalTranscript("first"))
        self.ctrl.dispatch(FinalTranscript("second"))
        self.assertEqual(len(self.timer.scheduled), 2)
        self.assertEqual(len(self.timer.cancelled), 1)


if __name__ == "__main__":
    unittest.main()
