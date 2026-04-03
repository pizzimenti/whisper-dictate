from __future__ import annotations

import tempfile
import threading
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import whisper_dictate.core.daemon as daemon_module
from whisper_dictate.config import DictationConfig
from whisper_dictate.constants import STATE_ERROR, STATE_IDLE, STATE_RECORDING, STATE_STARTING, STATE_TRANSCRIBING
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


class _BlockingStartStream(_DummyStream):
    def __init__(self, start_called: threading.Event, allow_start_return: threading.Event) -> None:
        super().__init__()
        self._start_called = start_called
        self._allow_start_return = allow_start_return

    def start(self) -> None:
        self._start_called.set()
        if not self._allow_start_return.wait(timeout=1.0):
            raise RuntimeError("stream start gate timed out")
        super().start()


class _CancelOnStartStream(_DummyStream):
    def __init__(self, daemon: DictationDaemon) -> None:
        super().__init__()
        self._daemon = daemon

    def start(self) -> None:
        self._daemon._cancel_start.set()
        super().start()


class _DummyThread:
    def __init__(self) -> None:
        self.join_timeouts: list[float | None] = []

    def join(self, timeout: float | None = None) -> None:
        self.join_timeouts.append(timeout)

    def is_alive(self) -> bool:
        return False


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
            self.assertIn(("state", STATE_STARTING), sink.events)
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
            self.assertIn(("state", STATE_STARTING), sink.events)
            self.assertNotIn(("state", STATE_RECORDING), sink.events)
            self.assertIn(("state", STATE_ERROR), sink.events)
            self.assertIn(("state", STATE_IDLE), sink.events)
            self.assertTrue(any(event[0] == "error" and event[1][0] == "audio_input_unavailable" for event in sink.events))

    def test_stop_during_startup_cancels_pending_recording(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_paths = RuntimePaths(
                state_file=Path(tmpdir) / "state",
                last_text_file=Path(tmpdir) / "last",
            )
            sink = _RecordingEventSink(events=[])
            stream = _DummyStream()
            resolver_called = threading.Event()
            allow_resolver_return = threading.Event()

            def input_device_resolver() -> tuple[str, bool]:
                resolver_called.set()
                self.assertTrue(allow_resolver_return.wait(timeout=1.0))
                return ("microphone", True)

            daemon = DictationDaemon(
                _make_config(runtime_paths),
                model=object(),
                runtime_paths=runtime_paths,
                event_sink=sink,
                stream_factory=lambda **kwargs: stream,
                input_device_resolver=input_device_resolver,
            )
            daemon._vad_worker = lambda: None  # type: ignore[method-assign]
            daemon._decode_worker = lambda: None  # type: ignore[method-assign]

            start_thread = threading.Thread(target=daemon._run_start_session, daemon=True)
            start_thread.start()
            self.assertTrue(resolver_called.wait(timeout=1.0))

            daemon._run_stop_session()
            allow_resolver_return.set()
            start_thread.join(timeout=1.0)

            self.assertFalse(start_thread.is_alive())
            self.assertEqual(daemon.get_state(), STATE_IDLE)
            self.assertEqual(read_state(runtime_paths.state_file), STATE_IDLE)
            self.assertFalse(stream.started)
            self.assertFalse(stream.stopped)
            self.assertFalse(stream.closed)
            self.assertNotIn(("state", STATE_RECORDING), sink.events)

    def test_stop_during_stream_start_closes_partial_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_paths = RuntimePaths(
                state_file=Path(tmpdir) / "state",
                last_text_file=Path(tmpdir) / "last",
            )
            sink = _RecordingEventSink(events=[])
            start_called = threading.Event()
            allow_start_return = threading.Event()
            stream = _BlockingStartStream(start_called, allow_start_return)
            daemon = DictationDaemon(
                _make_config(runtime_paths),
                model=object(),
                runtime_paths=runtime_paths,
                event_sink=sink,
                stream_factory=lambda **kwargs: stream,
                input_device_resolver=lambda: ("microphone", True),
            )
            daemon._vad_worker = lambda: None  # type: ignore[method-assign]
            daemon._decode_worker = lambda: None  # type: ignore[method-assign]

            start_thread = threading.Thread(target=daemon._run_start_session, daemon=True)
            start_thread.start()
            self.assertTrue(start_called.wait(timeout=1.0))

            daemon._run_stop_session()
            allow_start_return.set()
            start_thread.join(timeout=1.0)

            self.assertFalse(start_thread.is_alive())
            self.assertEqual(daemon.get_state(), STATE_IDLE)
            self.assertEqual(read_state(runtime_paths.state_file), STATE_IDLE)
            self.assertTrue(stream.started)
            self.assertTrue(stream.stopped)
            self.assertTrue(stream.closed)
            self.assertNotIn(("state", STATE_RECORDING), sink.events)

    def test_cancel_after_stream_start_does_not_enter_recording(self) -> None:
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
                stream_factory=lambda **kwargs: _CancelOnStartStream(daemon),
                input_device_resolver=lambda: ("microphone", True),
            )
            daemon._vad_worker = lambda: None  # type: ignore[method-assign]
            daemon._decode_worker = lambda: None  # type: ignore[method-assign]

            daemon._run_start_session()

            self.assertEqual(daemon.get_state(), STATE_IDLE)
            self.assertEqual(read_state(runtime_paths.state_file), STATE_IDLE)
            self.assertNotIn(("state", STATE_RECORDING), sink.events)

    def test_stop_waits_for_decode_worker_without_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_paths = RuntimePaths(
                state_file=Path(tmpdir) / "state",
                last_text_file=Path(tmpdir) / "last",
            )
            daemon = DictationDaemon(
                _make_config(runtime_paths),
                model=object(),
                runtime_paths=runtime_paths,
                event_sink=_RecordingEventSink(events=[]),
                stream_factory=lambda **kwargs: _DummyStream(),
                input_device_resolver=lambda: ("microphone", True),
            )
            daemon._recording = True
            daemon._handles.stream = _DummyStream()
            vad_thread = _DummyThread()
            decode_thread = _DummyThread()
            daemon._handles.vad_thread = vad_thread  # type: ignore[assignment]
            daemon._handles.decode_thread = decode_thread  # type: ignore[assignment]

            daemon._run_stop_session()

            self.assertEqual(vad_thread.join_timeouts, [None])
            self.assertEqual(decode_thread.join_timeouts, [None])

    def test_main_does_not_attach_event_sink_when_service_start_fails(self) -> None:
        events: list[str] = []

        class FakeDaemon:
            def __init__(self, config, model, runtime_paths, *, logger=None) -> None:
                del config, model, runtime_paths, logger
                events.append("daemon.init")

            def set_event_sink(self, sink) -> None:
                del sink
                events.append("daemon.set_event_sink")

            def shutdown(self) -> None:
                events.append("daemon.shutdown")

        class FakeService:
            def __init__(self, backend, *, logger=None) -> None:
                del backend, logger
                events.append("service.init")

            def start(self) -> None:
                events.append("service.start")
                raise RuntimeError("boom")

        runtime = {"device": "cpu", "compute_type": "int8", "cpu_threads": 1}
        config = SimpleNamespace(runtime_paths=object())

        with (
            patch("whisper_dictate.core.daemon._load_model_and_config", return_value=(config, object(), runtime)),
            patch("whisper_dictate.core.daemon.DictationDaemon", FakeDaemon),
            patch("whisper_dictate.service.dbus_service.SessionDbusService", FakeService),
        ):
            exit_code = daemon_module.main([])

        self.assertEqual(exit_code, 1)
        self.assertEqual(events, ["daemon.init", "service.init", "service.start", "daemon.shutdown"])
