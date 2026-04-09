from __future__ import annotations

import tempfile
import threading
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import kdictate.core.daemon as daemon_module
from kdictate.config import DictationConfig
from kdictate.constants import STATE_ERROR, STATE_IDLE, STATE_RECORDING, STATE_STARTING, STATE_TRANSCRIBING
from kdictate.core.daemon import DictationDaemon
from kdictate.runtime import RuntimePaths, read_last_text, read_state


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
    def __init__(self, *, alive_after_join: bool = False) -> None:
        self.join_timeouts: list[float | None] = []
        self._alive_after_join = alive_after_join

    def join(self, timeout: float | None = None) -> None:
        self.join_timeouts.append(timeout)

    def is_alive(self) -> bool:
        return self._alive_after_join


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
        energy_threshold=600.0,
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

    def test_stop_joins_workers_with_bounded_timeout(self) -> None:
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

            self.assertEqual(vad_thread.join_timeouts, [30.0])
            self.assertEqual(decode_thread.join_timeouts, [30.0])
            self.assertEqual(daemon.get_state(), STATE_IDLE)

    def test_stop_recovers_to_idle_when_decode_worker_does_not_exit(self) -> None:
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
                input_device_resolver=lambda: ("microphone", True),
            )
            daemon._recording = True
            daemon._handles.stream = _DummyStream()
            # Decode worker remains alive after join — simulates a wedged
            # CTranslate2 / OpenMP deadlock that the old timeout=None code
            # would have left blocked forever in STATE_TRANSCRIBING.
            wedged_decode = _DummyThread(alive_after_join=True)
            daemon._handles.vad_thread = _DummyThread()  # type: ignore[assignment]
            daemon._handles.decode_thread = wedged_decode  # type: ignore[assignment]

            # Capture references to the original session primitives so we
            # can assert they get rotated by the recovery path.
            old_audio_queue = daemon._audio_queue
            old_utterance_queue = daemon._utterance_queue
            old_stop_vad = daemon._stop_vad

            daemon._run_stop_session()

            # Daemon must not stay wedged in STATE_TRANSCRIBING; it should
            # surface a worker_join_timeout error and force back to IDLE so
            # subsequent Start/Stop calls are accepted.
            self.assertEqual(daemon.get_state(), STATE_IDLE)
            self.assertEqual(read_state(runtime_paths.state_file), STATE_IDLE)
            self.assertFalse(daemon._transcribing)
            self.assertIn(("state", STATE_TRANSCRIBING), sink.events)
            self.assertIn(("state", STATE_ERROR), sink.events)
            self.assertIn(("state", STATE_IDLE), sink.events)
            self.assertTrue(
                any(
                    event[0] == "error" and event[1][0] == "worker_join_timeout"
                    for event in sink.events
                )
            )

            # The leaked decode worker still references the old session
            # primitives. The daemon must rotate them so a future
            # _run_start_session reads/writes a different set — otherwise
            # _run_start_session's clear of self._stop_vad would also
            # clear the leaked worker's stop condition.
            self.assertIsNot(daemon._audio_queue, old_audio_queue)
            self.assertIsNot(daemon._utterance_queue, old_utterance_queue)
            self.assertIsNot(daemon._stop_vad, old_stop_vad)
            self.assertTrue(old_stop_vad.is_set())  # leaked worker still sees its old flag
            # Session generation must bump so a leaked _decode_worker
            # bails on its next iteration instead of consuming from /
            # publishing into the rotated new session.
            self.assertEqual(daemon._session_generation, 1)

    def test_decode_worker_drops_post_transcribe_text_after_rotation(self) -> None:
        """A leaked decode worker must drop transcripts after a session rotation.

        _decode_worker rereads self._utterance_queue each iteration and
        publishes via shared event-sink helpers, so without the
        per-session generation check a worker that outlived a wedge
        recovery would consume from the new rotated queue OR publish a
        stale transcription into the new session. The post-transcribe
        check guards both windows.

        This test simulates the realistic race window: the
        transcription function takes a long time and the daemon rotates
        the session in the middle of it. After transcribe returns, the
        decode worker must compare its captured generation against the
        current daemon generation and drop the text.
        """

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_paths = RuntimePaths(
                state_file=Path(tmpdir) / "state",
                last_text_file=Path(tmpdir) / "last",
            )
            sink = _RecordingEventSink(events=[])
            rotation_done = threading.Event()

            # Forward declaration so the closure can capture `daemon`.
            daemon: DictationDaemon  # set below

            def rotating_transcribe(*args: object, **kwargs: object) -> str:
                # Simulate the wedge-recovery rotation happening WHILE
                # the leaked worker is mid-transcribe.
                with daemon._lock:
                    daemon._session_generation += 1
                rotation_done.set()
                return "this transcription belongs to a stale session"

            daemon = DictationDaemon(
                _make_config(runtime_paths),
                model=object(),
                runtime_paths=runtime_paths,
                event_sink=sink,
                stream_factory=lambda **kwargs: _DummyStream(),
                input_device_resolver=lambda: ("microphone", True),
                transcription_fn=rotating_transcribe,
            )
            try:
                # Provide a "vad_thread" so the queue.Empty fallback path
                # doesn't take the "vad not alive after stop" early break.
                daemon._handles.vad_thread = _DummyThread()  # type: ignore[assignment]

                # Put one item to transcribe + a sentinel to exit cleanly
                # if the generation check ever fails to fire.
                daemon._utterance_queue.put((["dummy_pcm"], 1.0))
                daemon._utterance_queue.put(None)

                # Run _decode_worker synchronously in the test thread.
                daemon._decode_worker()

                # The transcription_fn was called and rotated the daemon.
                self.assertTrue(rotation_done.is_set())
                # The post-transcribe generation check must have dropped
                # the stale text. No partial transcript should have been
                # published into the new session.
                self.assertFalse(
                    any(e[0] == "partial" for e in sink.events),
                    f"unexpected partial transcript published: {sink.events}",
                )
            finally:
                daemon.shutdown()

    def test_request_stop_synchronously_cancels_blocked_start(self) -> None:
        """request_stop must set the cancel flag synchronously, not via the queue.

        With the serialized control thread, a Stop arriving while
        _run_start_session is blocked inside input_device_resolver(),
        stream construction, or stream.start() cannot wait behind the
        active task — it would miss every cancellation window. The
        synchronous lock-protected flag set bypasses the queue.
        """

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
            try:
                with daemon._lock:
                    daemon._starting = True
                    daemon._recording = False
                self.assertFalse(daemon._cancel_start.is_set())
                self.assertFalse(daemon._stop_vad.is_set())

                daemon.request_stop()

                # Must be set IMMEDIATELY, before the queued
                # _run_stop_session task ever runs.
                self.assertTrue(daemon._cancel_start.is_set())
                self.assertTrue(daemon._stop_vad.is_set())
            finally:
                daemon.shutdown()

    def test_request_methods_are_no_op_after_shutdown(self) -> None:
        """request_start / request_stop must drop work once shutdown begins.

        Without the _shutting_down fence, work enqueued during or after
        shutdown could race with the teardown and reopen the audio stream
        while the service is trying to exit.
        """

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

            daemon.shutdown()

            # request_*() after shutdown must drop the work silently
            # rather than enqueueing onto a dead control thread.
            daemon.request_start()
            daemon.request_stop()
            self.assertTrue(daemon._control_queue.empty())

    def test_shutdown_drains_pending_control_tasks(self) -> None:
        """shutdown must drain queued tasks so they cannot run during teardown.

        A queued request_start() that arrived just before the shutdown
        fence would otherwise run after shutdown began, racing with
        flag clearing and stream teardown.
        """

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

            ran: list[str] = []

            def fake_task() -> None:
                ran.append("ran")

            # Block the control worker on a sentinel-style task we
            # control, then queue some real work behind it. We want to
            # know what happens to that queued work when shutdown is
            # called before the control worker reaches it.
            release_gate = threading.Event()

            def gate_task() -> None:
                ran.append("gate")
                release_gate.wait(timeout=2.0)

            # Inject the gate task and a follow-up that should NEVER run.
            daemon._control_queue.put(gate_task)
            daemon._control_queue.put(fake_task)
            daemon._control_queue.put(fake_task)

            # Wait until the gate task is actively executing.
            for _ in range(50):
                if "gate" in ran:
                    break
                threading.Event().wait(0.01)
            self.assertIn("gate", ran)

            # Shutdown should fence further enqueues and drain the two
            # follow-up fake_task entries before posting the sentinel.
            shutdown_thread = threading.Thread(target=daemon.shutdown, daemon=True)
            shutdown_thread.start()
            release_gate.set()
            shutdown_thread.join(timeout=5.0)

            self.assertFalse(shutdown_thread.is_alive())
            # The two queued fake_task entries must have been drained, NOT
            # executed. Only the gate task ran.
            self.assertEqual(ran, ["gate"])

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
            patch("kdictate.core.daemon._load_model_and_config", return_value=(config, object(), runtime)),
            patch("kdictate.core.daemon.DictationDaemon", FakeDaemon),
            patch("kdictate.service.dbus_service.SessionDbusService", FakeService),
        ):
            exit_code = daemon_module.main([])

        self.assertEqual(exit_code, 1)
        self.assertEqual(events, ["daemon.init", "service.init", "service.start", "daemon.shutdown"])
