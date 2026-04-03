from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from whisper_dictate.config import DictationConfig
from whisper_dictate.constants import STATE_ERROR, STATE_IDLE, STATE_RECORDING, STATE_TRANSCRIBING
from whisper_dictate.core.daemon import DictationDaemon
from whisper_dictate.runtime import RuntimePaths, read_last_text, read_state


@dataclass
class _RecordingEventSink:
    events: list[tuple[str, object]]

    def state_changed(self, state: str) -> None:
        self.events.append(("state", state))

    def partial_transcript(self, text: str) -> None:
        self.events.append(("partial", text))

    def final_transcript(self, text: str) -> None:
        self.events.append(("final", text))

    def error_occurred(self, code: str, message: str) -> None:
        self.events.append(("error", (code, message)))


class _DummyStream:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True


def _make_config(runtime_paths: RuntimePaths) -> DictationConfig:
    return DictationConfig(
        model_dir=Path("."),
        language="en",
        sample_rate=16000,
        beam_size=1,
        condition_on_previous_text=False,
        vad_filter=False,
        no_speech_threshold=0.6,
        cpu_threads=1,
        compute_type="int8",
        block_ms=30,
        energy_threshold=300.0,
        silence_ms=220,
        min_speech_ms=180,
        start_speech_ms=90,
        max_utterance_s=2.5,
        runtime_paths=runtime_paths,
    )


class DictationDaemonTest(unittest.TestCase):
    def test_start_stop_emits_state_partial_and_final_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_paths = RuntimePaths(
                state_file=Path(tmpdir) / "state",
                last_text_file=Path(tmpdir) / "last",
            )
            sink = _RecordingEventSink(events=[])
            stream = _DummyStream()
            daemon = DictationDaemon(
                _make_config(runtime_paths),
                model=object(),
                runtime_paths=runtime_paths,
                event_sink=sink,
                stream_factory=lambda **kwargs: stream,
                input_device_resolver=lambda: ("microphone", True),
                transcription_fn=lambda *args, **kwargs: "hello",
            )
            daemon._vad_worker = lambda: None  # type: ignore[method-assign]
            daemon._decode_worker = lambda: None  # type: ignore[method-assign]

            daemon._run_start_session()
            self.assertEqual(daemon.get_state(), STATE_RECORDING)
            self.assertTrue(stream.started)
            self.assertEqual(read_state(runtime_paths.state_file), STATE_RECORDING)

            daemon._record_partial_text("hello")
            daemon._run_stop_session()

            self.assertEqual(daemon.get_state(), STATE_IDLE)
            self.assertTrue(stream.stopped)
            self.assertTrue(stream.closed)
            self.assertEqual(read_state(runtime_paths.state_file), STATE_IDLE)
            self.assertEqual(read_last_text(runtime_paths.last_text_file), "hello")
            self.assertIn(("state", STATE_RECORDING), sink.events)
            self.assertIn(("state", STATE_TRANSCRIBING), sink.events)
            self.assertIn(("state", STATE_IDLE), sink.events)
            self.assertIn(("partial", "hello"), sink.events)
            self.assertIn(("final", "hello"), sink.events)

    def test_start_failure_emits_audio_error_and_recovers_to_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_paths = RuntimePaths(
                state_file=Path(tmpdir) / "state",
                last_text_file=Path(tmpdir) / "last",
            )
            sink = _RecordingEventSink(events=[])
            daemon = DictationDaemon(
                _make_config(runtime_paths),
                model=object(),
                runtime_paths=runtime_paths,
                event_sink=sink,
                stream_factory=lambda **kwargs: _DummyStream(),
                input_device_resolver=lambda: ("none", False),
            )

            daemon._run_start_session()

            self.assertEqual(daemon.get_state(), STATE_IDLE)
            self.assertEqual(read_state(runtime_paths.state_file), STATE_IDLE)
            self.assertIn(("state", STATE_RECORDING), sink.events)
            self.assertIn(("state", STATE_ERROR), sink.events)
            self.assertIn(("state", STATE_IDLE), sink.events)
            self.assertTrue(any(event[0] == "error" and event[1][0] == "audio_input_unavailable" for event in sink.events))
