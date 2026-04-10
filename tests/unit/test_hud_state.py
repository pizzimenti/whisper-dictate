"""Unit tests for the HUD state reducer."""

from __future__ import annotations

import unittest

from kdictate.constants import (
    STATE_ERROR,
    STATE_IDLE,
    STATE_RECORDING,
    STATE_STARTING,
    STATE_TRANSCRIBING,
)
from kdictate.hud.state import (
    CommitAckExpired,
    DaemonAppeared,
    DaemonStateChanged,
    DaemonVanished,
    ErrorOccurred,
    FinalTranscript,
    HudModel,
    HudPhase,
    PartialTranscript,
    SnapshotReceived,
    reduce,
)


class ReduceDaemonLifecycleTest(unittest.TestCase):
    """DaemonAppeared / DaemonVanished events."""

    def test_daemon_appeared_sets_available(self) -> None:
        m = reduce(HudModel(), DaemonAppeared())
        self.assertTrue(m.daemon_available)
        self.assertEqual(m.phase, HudPhase.HIDDEN)

    def test_daemon_vanished_resets_everything(self) -> None:
        m = HudModel(
            phase=HudPhase.PARTIAL,
            partial_text="hello",
            daemon_available=True,
        )
        m = reduce(m, DaemonVanished())
        self.assertEqual(m, HudModel())

    def test_daemon_vanished_clears_committing(self) -> None:
        m = HudModel(phase=HudPhase.COMMITTING, commit_text="done", daemon_available=True)
        m = reduce(m, DaemonVanished())
        self.assertEqual(m.phase, HudPhase.HIDDEN)
        self.assertEqual(m.commit_text, "")


class ReduceDaemonStateTest(unittest.TestCase):
    """DaemonStateChanged event mapping to HUD phases."""

    def _available(self) -> HudModel:
        return HudModel(daemon_available=True)

    def test_idle_hides_hud(self) -> None:
        m = reduce(self._available(), DaemonStateChanged(STATE_IDLE))
        self.assertEqual(m.phase, HudPhase.HIDDEN)

    def test_starting_shows_starting(self) -> None:
        m = reduce(self._available(), DaemonStateChanged(STATE_STARTING))
        self.assertEqual(m.phase, HudPhase.STARTING)

    def test_recording_shows_listening(self) -> None:
        m = reduce(self._available(), DaemonStateChanged(STATE_RECORDING))
        self.assertEqual(m.phase, HudPhase.LISTENING)

    def test_transcribing_shows_transcribing(self) -> None:
        m = reduce(self._available(), DaemonStateChanged(STATE_TRANSCRIBING))
        self.assertEqual(m.phase, HudPhase.TRANSCRIBING)

    def test_error_shows_error(self) -> None:
        m = reduce(self._available(), DaemonStateChanged(STATE_ERROR))
        self.assertEqual(m.phase, HudPhase.ERROR)

    def test_unknown_state_shows_error(self) -> None:
        m = reduce(self._available(), DaemonStateChanged("bogus"))
        self.assertEqual(m.phase, HudPhase.ERROR)
        self.assertIn("bogus", m.error_message)

    def test_idle_preserves_committing_phase(self) -> None:
        m = HudModel(
            phase=HudPhase.COMMITTING,
            commit_text="done",
            daemon_available=True,
        )
        m = reduce(m, DaemonStateChanged(STATE_IDLE))
        self.assertEqual(m.phase, HudPhase.COMMITTING)
        self.assertEqual(m.commit_text, "done")

    def test_recording_preserves_visible_partial(self) -> None:
        m = HudModel(
            phase=HudPhase.PARTIAL,
            partial_text="hello",
            daemon_available=True,
        )
        m = reduce(m, DaemonStateChanged(STATE_RECORDING))
        self.assertEqual(m.phase, HudPhase.PARTIAL)
        self.assertEqual(m.partial_text, "hello")

    def test_recording_does_not_preserve_empty_partial(self) -> None:
        m = HudModel(
            phase=HudPhase.PARTIAL,
            partial_text="",
            daemon_available=True,
        )
        m = reduce(m, DaemonStateChanged(STATE_RECORDING))
        self.assertEqual(m.phase, HudPhase.LISTENING)

    def test_starting_clears_partial_text(self) -> None:
        m = HudModel(
            phase=HudPhase.PARTIAL,
            partial_text="old",
            daemon_available=True,
        )
        m = reduce(m, DaemonStateChanged(STATE_STARTING))
        self.assertEqual(m.partial_text, "")

    def test_recording_clears_stale_partial_from_non_partial_phase(self) -> None:
        m = HudModel(
            phase=HudPhase.HIDDEN,
            partial_text="stale",
            daemon_available=True,
        )
        m = reduce(m, DaemonStateChanged(STATE_RECORDING))
        self.assertEqual(m.partial_text, "")


class ReducePartialTranscriptTest(unittest.TestCase):
    """PartialTranscript event handling."""

    def _listening(self) -> HudModel:
        return HudModel(phase=HudPhase.LISTENING, daemon_available=True)

    def test_partial_transitions_to_partial_phase(self) -> None:
        m = reduce(self._listening(), PartialTranscript("hello"))
        self.assertEqual(m.phase, HudPhase.PARTIAL)
        self.assertEqual(m.partial_text, "hello")

    def test_empty_partial_stays_listening(self) -> None:
        m = reduce(self._listening(), PartialTranscript("   "))
        self.assertEqual(m.phase, HudPhase.LISTENING)
        self.assertEqual(m.partial_text, "")

    def test_partial_replaces_previous(self) -> None:
        m = HudModel(phase=HudPhase.PARTIAL, partial_text="old", daemon_available=True)
        m = reduce(m, PartialTranscript("new"))
        self.assertEqual(m.partial_text, "new")

    def test_partial_strips_whitespace(self) -> None:
        m = reduce(self._listening(), PartialTranscript("  hello world  "))
        self.assertEqual(m.partial_text, "hello world")

    def test_partial_ignored_when_hidden(self) -> None:
        m = HudModel(phase=HudPhase.HIDDEN, daemon_available=True)
        m2 = reduce(m, PartialTranscript("hello"))
        self.assertEqual(m2, m)

    def test_partial_ignored_when_committing(self) -> None:
        m = HudModel(phase=HudPhase.COMMITTING, commit_text="done", daemon_available=True)
        m2 = reduce(m, PartialTranscript("stale"))
        self.assertEqual(m2, m)

    def test_partial_accepted_during_transcribing(self) -> None:
        m = HudModel(phase=HudPhase.TRANSCRIBING, daemon_available=True)
        m = reduce(m, PartialTranscript("late partial"))
        self.assertEqual(m.phase, HudPhase.PARTIAL)
        self.assertEqual(m.partial_text, "late partial")


class ReduceFinalTranscriptTest(unittest.TestCase):
    """FinalTranscript event handling."""

    def test_final_transitions_to_committing(self) -> None:
        m = HudModel(phase=HudPhase.PARTIAL, partial_text="hello", daemon_available=True)
        m = reduce(m, FinalTranscript("hello world"))
        self.assertEqual(m.phase, HudPhase.COMMITTING)
        self.assertEqual(m.commit_text, "hello world")
        self.assertEqual(m.partial_text, "")

    def test_empty_final_clears_partial(self) -> None:
        m = HudModel(phase=HudPhase.PARTIAL, partial_text="old", daemon_available=True)
        m = reduce(m, FinalTranscript("   "))
        self.assertEqual(m.partial_text, "")
        self.assertNotEqual(m.phase, HudPhase.COMMITTING)

    def test_final_strips_whitespace(self) -> None:
        m = HudModel(phase=HudPhase.LISTENING, daemon_available=True)
        m = reduce(m, FinalTranscript("  result  "))
        self.assertEqual(m.commit_text, "result")

    def test_second_final_replaces_first(self) -> None:
        m = HudModel(phase=HudPhase.COMMITTING, commit_text="first", daemon_available=True)
        m = reduce(m, FinalTranscript("second"))
        self.assertEqual(m.commit_text, "second")


class ReduceErrorTest(unittest.TestCase):
    """ErrorOccurred event handling."""

    def test_error_transitions_to_error_phase(self) -> None:
        m = HudModel(phase=HudPhase.LISTENING, daemon_available=True)
        m = reduce(m, ErrorOccurred("mic_fail", "Microphone not found"))
        self.assertEqual(m.phase, HudPhase.ERROR)
        self.assertEqual(m.error_message, "Microphone not found")
        self.assertEqual(m.partial_text, "")


class ReduceCommitAckExpiredTest(unittest.TestCase):
    """CommitAckExpired timer event."""

    def test_expired_hides_from_committing(self) -> None:
        m = HudModel(phase=HudPhase.COMMITTING, commit_text="done", daemon_available=True)
        m = reduce(m, CommitAckExpired())
        self.assertEqual(m.phase, HudPhase.HIDDEN)
        self.assertEqual(m.commit_text, "")

    def test_expired_noop_when_not_committing(self) -> None:
        m = HudModel(phase=HudPhase.LISTENING, daemon_available=True)
        m2 = reduce(m, CommitAckExpired())
        self.assertEqual(m2, m)


class ReduceFullLifecycleTest(unittest.TestCase):
    """End-to-end lifecycle sequences."""

    def test_full_dictation_cycle(self) -> None:
        m = HudModel()

        m = reduce(m, DaemonAppeared())
        self.assertTrue(m.daemon_available)

        m = reduce(m, DaemonStateChanged(STATE_STARTING))
        self.assertEqual(m.phase, HudPhase.STARTING)

        m = reduce(m, DaemonStateChanged(STATE_RECORDING))
        self.assertEqual(m.phase, HudPhase.LISTENING)

        m = reduce(m, PartialTranscript("hello"))
        self.assertEqual(m.phase, HudPhase.PARTIAL)

        m = reduce(m, PartialTranscript("hello world"))
        self.assertEqual(m.partial_text, "hello world")

        m = reduce(m, DaemonStateChanged(STATE_TRANSCRIBING))
        self.assertEqual(m.phase, HudPhase.TRANSCRIBING)

        m = reduce(m, FinalTranscript("hello world"))
        self.assertEqual(m.phase, HudPhase.COMMITTING)

        m = reduce(m, DaemonStateChanged(STATE_IDLE))
        self.assertEqual(m.phase, HudPhase.COMMITTING)

        m = reduce(m, CommitAckExpired())
        self.assertEqual(m.phase, HudPhase.HIDDEN)

    def test_new_session_overrides_commit_ack(self) -> None:
        m = HudModel(
            phase=HudPhase.COMMITTING,
            commit_text="done",
            daemon_available=True,
        )
        m = reduce(m, DaemonStateChanged(STATE_STARTING))
        self.assertEqual(m.phase, HudPhase.STARTING)

        m = reduce(m, CommitAckExpired())
        self.assertEqual(m.phase, HudPhase.STARTING)


class ReduceSnapshotTest(unittest.TestCase):
    """SnapshotReceived event for restart-tolerant seeding."""

    def test_snapshot_seeds_recording_with_partial(self) -> None:
        m = HudModel(daemon_available=True)
        m = reduce(m, SnapshotReceived("recording", "hello", "", "", ""))
        self.assertEqual(m.phase, HudPhase.PARTIAL)
        self.assertEqual(m.partial_text, "hello")

    def test_snapshot_seeds_idle(self) -> None:
        m = HudModel(daemon_available=True)
        m = reduce(m, SnapshotReceived("idle", "", "last text", "", ""))
        self.assertEqual(m.phase, HudPhase.HIDDEN)

    def test_snapshot_seeds_recording_without_partial(self) -> None:
        m = HudModel(daemon_available=True)
        m = reduce(m, SnapshotReceived("recording", "", "", "", ""))
        self.assertEqual(m.phase, HudPhase.LISTENING)
        self.assertEqual(m.partial_text, "")

    def test_snapshot_seeds_error_with_message(self) -> None:
        m = HudModel(daemon_available=True)
        m = reduce(m, SnapshotReceived("error", "", "", "mic_fail", "No mic"))
        self.assertEqual(m.phase, HudPhase.ERROR)
        self.assertEqual(m.error_message, "No mic")

    def test_snapshot_seeds_transcribing_with_partial(self) -> None:
        m = HudModel(daemon_available=True)
        m = reduce(m, SnapshotReceived("transcribing", "hello world", "", "", ""))
        self.assertEqual(m.phase, HudPhase.PARTIAL)
        self.assertEqual(m.partial_text, "hello world")


if __name__ == "__main__":
    unittest.main()
