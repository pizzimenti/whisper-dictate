"""Core dictation daemon logic and process entrypoint."""

from __future__ import annotations

import logging
import queue
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib

from kdictate.audio_common import (
    AUDIO_QUEUE_MAXSIZE,
    UTTERANCE_QUEUE_MAXSIZE,
    VADConfig,
    VADSegmenter,
    load_whisper_model,
)
from kdictate.backend import TranscriptionBackend, create_cpu_backend
from kdictate.config import DictationConfig, parse_args
from kdictate.constants import STATE_ERROR, STATE_IDLE, STATE_RECORDING, STATE_STARTING, STATE_TRANSCRIBING
from kdictate.core.audio import resolve_default_input_device
from kdictate.exceptions import AudioInputError, ConfigurationError, TranscriptionError
from kdictate.logging_utils import configure_logging, get_propagating_child
from kdictate.runtime import RuntimePaths, write_last_text, write_state
from kdictate.runtime_profile import resolve_runtime, set_thread_env


class DaemonEventSink(Protocol):
    """Observer interface used to publish daemon events to a transport layer."""

    def state_changed(self, state: str) -> None: ...

    def partial_transcript(self, text: str) -> None: ...

    def final_transcript(self, text: str) -> None: ...

    def error_occurred(self, code: str, message: str) -> None: ...


class _NullEventSink:
    """No-op sink used when the daemon runs without an attached transport."""

    def state_changed(self, _state: str) -> None:
        pass

    def partial_transcript(self, _text: str) -> None:
        pass

    def final_transcript(self, _text: str) -> None:
        pass

    def error_occurred(self, _code: str, _message: str) -> None:
        pass


@dataclass(slots=True)
class _ThreadHandles:
    """Bookkeeping for background workers."""

    vad_thread: threading.Thread | None = None
    decode_thread: threading.Thread | None = None
    stream: Any | None = None


class _WorkerJoinTimeoutError(RuntimeError):
    """Raised when a worker fails to stop within the expected window."""


def load_model(config: DictationConfig) -> tuple[Any, dict[str, Any]]:
    """Load the configured faster-whisper model and runtime profile."""

    runtime = resolve_runtime("cpu", config.compute_type, config.cpu_threads)
    set_thread_env(runtime["cpu_threads"])

    if not config.model_dir.exists():
        raise ConfigurationError(f"Model directory not found: {config.model_dir}")

    logging.getLogger("kdictate.core").info("loading model from %s", config.model_dir)
    model = load_whisper_model(
        config.model_dir,
        device=runtime["device"],
        compute_type=runtime["compute_type"],
        cpu_threads=runtime["cpu_threads"],
        num_workers=1,
    )
    return model, runtime


class DictationDaemon:
    """Own microphone capture, VAD segmentation, transcription, and state."""

    def __init__(
        self,
        config: DictationConfig,
        backend: TranscriptionBackend,
        runtime_paths: RuntimePaths,
        *,
        event_sink: DaemonEventSink | None = None,
        logger: logging.Logger | None = None,
        stream_factory: Callable[..., Any] | None = None,
        input_device_resolver: Callable[[], tuple[str, bool]] = resolve_default_input_device,
        notify_error_fn: Callable[[str, str], None] | None = None,
    ) -> None:
        self.config = config
        self._backend = backend
        self.runtime_paths = runtime_paths
        self._event_sink = event_sink or _NullEventSink()
        self._logger = logger or configure_logging("kdictate.daemon.core")
        self._stream_factory = stream_factory
        self._input_device_resolver = input_device_resolver
        # Desktop-notification side-effect, injected so tests can replace
        # it with a no-op or a recorder. Default uses real notify-send.
        self._notify_error_fn = notify_error_fn or self._send_desktop_notification
        self._lock = threading.RLock()
        self._recording = False
        self._starting = False
        self._transcribing = False
        self._state = STATE_IDLE
        self._last_text = ""
        self._streamed_text: list[str] = []
        self._audio_queue: queue.Queue[Any] = queue.Queue(maxsize=AUDIO_QUEUE_MAXSIZE)
        self._utterance_queue: queue.Queue[Any] = queue.Queue(maxsize=UTTERANCE_QUEUE_MAXSIZE)
        self._stop_vad = threading.Event()
        self._cancel_start = threading.Event()
        self._pending_start = threading.Event()
        self._last_error_notify_time: float = 0.0
        self._handles = _ThreadHandles()
        # Session generation: bumped every time the wedge-recovery path
        # rotates the session primitives. Each decode worker captures the
        # generation at start and bails on mismatch so a leaked worker
        # cannot consume from the rotated queue or publish into a
        # different session via shared event-sink helpers.
        self._session_generation = 0

        # A single dedicated control-plane thread serializes start/stop
        # requests so a rapid Ctrl+Space burst (or a misbehaving client)
        # cannot spawn an unbounded number of threads. Each request is a
        # Callable enqueued onto _control_queue; the worker pulls and runs
        # them sequentially. shutdown() drains the queue and posts a None
        # sentinel.
        self._control_queue: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self._shutting_down = threading.Event()
        self._control_thread = threading.Thread(
            target=self._control_worker,
            name="kdictate-control",
            daemon=True,
        )
        self._control_thread.start()

        self._write_state(STATE_IDLE)
        write_last_text(self.runtime_paths.last_text_file, "")

    def _control_worker(self) -> None:
        """Serialize start/stop requests on a single thread."""

        while True:
            task = self._control_queue.get()
            if task is None:
                return
            try:
                task()
            except Exception:  # noqa: BLE001
                # Safety net: if a start/stop session function raises past
                # its own internal error handlers (a very narrow window —
                # both functions have comprehensive guards), the daemon
                # could be left with a live audio stream, live VAD/decode
                # workers, AND _starting / _transcribing stuck True. Tear
                # the session resources down BEFORE flipping flags so the
                # daemon never reports STATE_IDLE while still holding the
                # mic or leaking workers. Reset the flags, surface a
                # STATE_ERROR event, and force back to STATE_IDLE so the
                # daemon stays responsive.
                self._logger.exception("control task failed; tearing down session and resetting flags")

                # Best-effort teardown of any live session resources. Each
                # step is itself wrapped so a failure inside teardown can
                # never prevent the recovery state transition below.
                try:
                    self._stop_vad.set()
                    self._cancel_start.set()
                    stream = self._handles.stream
                    self._handles.stream = None
                    self._close_stream(stream)
                    # Drop worker handles; the workers themselves will
                    # exit on their own next iteration via the stop flag.
                    # We deliberately do NOT join them here — that's the
                    # job of the wedge-recovery path in _run_stop_session,
                    # and joining inside the control worker could block
                    # the entire daemon if a worker is wedged.
                    self._handles.vad_thread = None
                    self._handles.decode_thread = None
                    # Bump the session generation so any worker that
                    # outlives this teardown (and rereads daemon state)
                    # will see a mismatch and bail.
                    with self._lock:
                        self._session_generation += 1
                except Exception:  # noqa: BLE001
                    self._logger.exception("safety net teardown failed")

                with self._lock:
                    self._starting = False
                    self._transcribing = False
                    # _recording is reset here too for defense in depth.
                    # _run_start_session / _run_stop_session both reset it
                    # in their own except blocks today, but if a future
                    # refactor lets an exception escape with _recording
                    # still True, every subsequent Start would be silently
                    # rejected as "daemon is already active" with no
                    # diagnostic.
                    self._recording = False
                self._pending_start.clear()
                self._cancel_start.clear()
                try:
                    self._emit_error("control_task_failed", "Control task raised unexpectedly")
                    self._write_state(STATE_ERROR)
                    self._write_state(STATE_IDLE)
                except Exception:  # noqa: BLE001
                    self._logger.exception("control task error reporting failed")

    def set_event_sink(self, event_sink: DaemonEventSink) -> None:
        """Attach or replace the transport that receives daemon events."""

        self._event_sink = event_sink

    def _write_state(self, state: str) -> None:
        """Persist and publish a state transition."""

        with self._lock:
            self._state = state
        write_state(self.runtime_paths.state_file, state)
        self._logger.info("state changed to %s", state)
        self._event_sink.state_changed(state)

    _ERROR_NOTIFY_COOLDOWN_S: float = 10.0

    def _emit_error(self, code: str, message: str) -> None:
        """Publish a structured failure event."""

        self._logger.error("%s: %s", code, message)
        self._event_sink.error_occurred(code, message)

    def _notify_error(self, summary: str, body: str) -> None:
        """Dispatch a user-facing error notification through the injected hook.

        Rate-limited so rapid repeated toggles don't spam the desktop.
        Tests inject a no-op or recording hook via ``notify_error_fn`` so
        running the unit suite never fires real KDE notifications.
        """

        now = time.monotonic()
        if now - self._last_error_notify_time < self._ERROR_NOTIFY_COOLDOWN_S:
            return
        self._last_error_notify_time = now
        try:
            self._notify_error_fn(summary, body)
        except Exception:  # noqa: BLE001
            self._logger.exception("desktop notification hook raised")

    def _send_desktop_notification(self, summary: str, body: str) -> None:
        """Default notify-send shell-out used by the production daemon.

        Silently skipped if notify-send is not on PATH.
        """

        notify_send = shutil.which("notify-send")
        if notify_send is None:
            return
        try:
            subprocess.Popen(
                [notify_send, "--app-name=KDictate", "--icon=audio-input-microphone",
                 "--urgency=normal", summary, body],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass

    def _reset_session_buffers(self) -> None:
        """Clear transient queues and accumulated transcript fragments."""

        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except queue.Empty:
                break
        while not self._utterance_queue.empty():
            try:
                self._utterance_queue.get_nowait()
            except queue.Empty:
                break
        self._streamed_text = []
        self._pending_start.clear()

    def _record_partial_text(self, text: str) -> None:
        """Append and publish a cumulative partial transcript."""

        if not text:
            return
        with self._lock:
            self._streamed_text.append(text)
            cumulative = " ".join(self._streamed_text).strip()
        self._logger.info("partial transcript emitted (%d chars)", len(cumulative))
        self._event_sink.partial_transcript(cumulative)

    def _finalize_text(self) -> str:
        """Persist and publish the current full transcript."""

        with self._lock:
            text = " ".join(self._streamed_text).strip()
            self._last_text = text
        write_last_text(self.runtime_paths.last_text_file, text)
        self._logger.info("final transcript emitted (%d chars)", len(text))
        if text:
            self._event_sink.final_transcript(text)
        return text

    def _close_stream(self, stream: Any | None) -> None:
        """Stop and close a sounddevice stream if one is present."""

        if stream is None:
            return
        for method_name in ("stop", "close"):
            method = getattr(stream, method_name, None)
            if method is None:
                continue
            try:
                method()
            except Exception:  # noqa: BLE001
                self._logger.exception("stream %s failed", method_name)

    def _join_worker(
        self,
        thread: threading.Thread | None,
        name: str,
        *,
        timeout: float | None,
        require_exit: bool,
    ) -> None:
        """Join a worker thread and optionally require that it exits."""

        if thread is None:
            return

        self._logger.info("waiting for %s worker to exit", name)
        thread.join(timeout=timeout)
        if require_exit and thread.is_alive():
            raise _WorkerJoinTimeoutError(f"{name} worker did not exit cleanly")

    def _cleanup_start_handles(self) -> None:
        """Stop any partially-started capture pipeline and clear worker handles."""

        self._stop_vad.set()
        stream = self._handles.stream
        self._handles.stream = None
        self._close_stream(stream)
        try:
            self._join_worker(self._handles.vad_thread, "vad", timeout=5.0, require_exit=True)
            self._join_worker(self._handles.decode_thread, "decode", timeout=5.0, require_exit=True)
        except _WorkerJoinTimeoutError as join_exc:
            self._logger.error("%s", join_exc)
        finally:
            self._handles.vad_thread = None
            self._handles.decode_thread = None

    def _cancel_pending_start(self) -> None:
        """Tear down a startup attempt that was cancelled before recording went live."""

        self._cleanup_start_handles()
        with self._lock:
            self._starting = False
            self._cancel_start.clear()
        self._logger.info("recording start cancelled before activation")
        self._write_state(STATE_IDLE)

    def _build_stream(self) -> Any:
        """Build the input stream using the configured or default factory."""

        if self._stream_factory is not None:
            return self._stream_factory(
                samplerate=self.config.sample_rate,
                channels=1,
                dtype="int16",
                blocksize=max(1, int(self.config.sample_rate * self.config.block_ms / 1000)),
                callback=self._input_callback,
            )

        import sounddevice as sd

        return sd.InputStream(
            samplerate=self.config.sample_rate,
            channels=1,
            dtype="int16",
            blocksize=max(1, int(self.config.sample_rate * self.config.block_ms / 1000)),
            callback=self._input_callback,
        )

    def _input_callback(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        """Receive audio blocks from the capture stream."""

        del frames, time_info, status
        with self._lock:
            if not self._recording:
                return
        chunk = indata[:, 0].copy()
        try:
            self._audio_queue.put_nowait(chunk)
        except queue.Full:
            self._logger.warning("audio queue full; dropping block")

    def _vad_worker(self) -> None:
        """Segment audio into utterances using the existing VAD helper."""

        vad = VADSegmenter(
            config=VADConfig(
                sample_rate=self.config.sample_rate,
                block_ms=self.config.block_ms,
                energy_threshold=self.config.energy_threshold,
                silence_ms=self.config.silence_ms,
                min_speech_ms=self.config.min_speech_ms,
                start_speech_ms=self.config.start_speech_ms,
                max_utterance_s=self.config.max_utterance_s,
            ),
            audio_queue=self._audio_queue,
            utterance_queue=self._utterance_queue,
            stop_event=self._stop_vad,
        )
        vad.run()

    def _decode_worker(self) -> None:
        """Transcribe each utterance and publish cumulative partial text."""

        # Snapshot session-scoped objects so a leaked worker (one that
        # outlived its _run_stop_session join timeout) reads from the OLD
        # queue/event and bails on generation mismatch instead of
        # consuming from the rotated NEW queue or publishing into a
        # different session via shared event-sink helpers like
        # _record_partial_text and _emit_error.
        session_generation = self._session_generation
        utterance_queue = self._utterance_queue
        stop_vad = self._stop_vad

        while True:
            # Generation mismatch means the daemon has rotated the
            # session primitives (wedge recovery or control-worker
            # teardown). Stop processing immediately so we never publish
            # transcripts into a different session.
            if self._session_generation != session_generation:
                self._logger.warning(
                    "decode worker exiting due to session rotation (gen %d -> %d)",
                    session_generation,
                    self._session_generation,
                )
                return

            try:
                item = utterance_queue.get(timeout=1.0)
            except queue.Empty:
                # Defensive exit: if a stop has been requested and the VAD
                # worker is no longer alive, the sentinel will never arrive
                # (e.g. VAD raised before reaching its put(None)). Break out
                # so the decode thread does not wedge _run_stop_session.
                if stop_vad.is_set():
                    vad_thread = self._handles.vad_thread
                    if vad_thread is None or not vad_thread.is_alive():
                        self._logger.warning(
                            "decode worker exiting without sentinel "
                            "(vad worker not alive after stop)"
                        )
                        break
                continue
            if item is None:
                break
            pcm_chunks, audio_seconds = item
            pending = utterance_queue.qsize()
            try:
                t0 = time.monotonic()
                text = self._backend.transcribe(pcm_chunks, audio_seconds)
                decode_s = time.monotonic() - t0
                # One more generation check before publishing — the
                # rotation may have happened during the (potentially
                # long) transcribe call.
                if self._session_generation != session_generation:
                    self._logger.warning(
                        "decode worker dropping post-transcribe text after session rotation"
                    )
                    return
                self._logger.info(
                    "decode %.1fs audio in %.1fs (%.2fx RT, %d queued)",
                    audio_seconds, decode_s, decode_s / max(audio_seconds, 0.01), pending,
                )
                if text:
                    self._record_partial_text(text)
            except Exception as exc:  # noqa: BLE001
                if self._session_generation != session_generation:
                    # Don't surface a stale-session transcription error
                    # into the new session.
                    return
                error = TranscriptionError(str(exc))
                self._emit_error("transcription_failed", str(error))
                self._logger.exception("decode worker failed")

    def _run_start_session(self) -> None:
        """Start the capture, VAD, and decode pipeline."""

        with self._lock:
            if self._recording or self._starting:
                self._logger.info("start ignored because the daemon is already active")
                return
            if self._transcribing:
                # Transcription is still draining from the previous session; defer
                # the start until _run_stop_session sets the pending-start event.
                self._pending_start.set()
                self._logger.info("start deferred until transcription completes")
                return
            self._starting = True
            self._cancel_start.clear()
            self._reset_session_buffers()
            self._handles = _ThreadHandles()
            self._stop_vad.clear()

        # Publish STATE_STARTING immediately so the IBus frontend and CLI can
        # show a "starting" state during the mic-validation window.
        self._write_state(STATE_STARTING)
        try:
            mic_name, mic_usable = self._input_device_resolver()
        except Exception as exc:  # noqa: BLE001
            # Resolver raised — treat the same as an unusable device.
            with self._lock:
                self._starting = False
                self._cancel_start.clear()
            self._write_state(STATE_ERROR)
            self._emit_error("audio_input_unavailable", str(exc))
            self._notify_error("Microphone unavailable", str(exc))
            self._write_state(STATE_IDLE)
            return
        if not mic_usable:
            with self._lock:
                self._starting = False
                self._cancel_start.clear()
            error = AudioInputError(f"No usable input device: {mic_name}")
            self._write_state(STATE_ERROR)
            self._emit_error("audio_input_unavailable", str(error))
            self._notify_error("Microphone unavailable", f"Default source is {mic_name}, which is not an input device.")
            self._write_state(STATE_IDLE)
            return

        # Check for a stop that arrived while mic validation was in flight.
        if self._cancel_start.is_set():
            with self._lock:
                self._starting = False
                self._cancel_start.clear()
            self._logger.info("recording start cancelled before activation")
            self._write_state(STATE_IDLE)
            return

        # Start decode before VAD so the decode worker is ready to consume
        # utterances the moment VAD enqueues them.
        self._handles.decode_thread = threading.Thread(
            target=self._decode_worker,
            name="kdictate-decode",
            daemon=True,
        )
        self._handles.vad_thread = threading.Thread(
            target=self._vad_worker,
            name="kdictate-vad",
            daemon=True,
        )
        self._handles.decode_thread.start()
        self._handles.vad_thread.start()

        # Another cancellation window: stop could arrive between worker start
        # and stream start.  Check before allocating the audio device.
        if self._cancel_start.is_set():
            self._cleanup_start_handles()
            with self._lock:
                self._starting = False
                self._cancel_start.clear()
            self._logger.info("recording start cancelled before activation")
            self._write_state(STATE_IDLE)
            return

        try:
            stream = self._build_stream()
            self._handles.stream = stream

            # Check between build and start — some backends open the device on start.
            if self._cancel_start.is_set():
                self._cancel_pending_start()
                return
            stream.start()

            # Final race window: stop could arrive immediately after stream.start().
            # Flip _recording inside the lock so _run_stop_session sees a consistent
            # view — it will not act if _recording is False.
            if self._cancel_start.is_set():
                self._cancel_pending_start()
                return
            with self._lock:
                if self._cancel_start.is_set():
                    self._starting = False
                    self._recording = False
                else:
                    self._recording = True
                    self._starting = False
            if self._cancel_start.is_set():
                self._cancel_pending_start()
                return

            write_last_text(self.runtime_paths.last_text_file, "")
            self._write_state(STATE_RECORDING)
            self._logger.info("recording started on %s", mic_name)
        except Exception as exc:  # noqa: BLE001
            self._logger.exception("recording start failed")
            self._emit_error("recording_start_failed", str(exc))
            self._cleanup_start_handles()
            with self._lock:
                self._starting = False
                self._cancel_start.clear()
                self._recording = False
                self._transcribing = False
            self._write_state(STATE_ERROR)
            self._write_state(STATE_IDLE)

    def _run_stop_session(self) -> None:
        """Stop capture, flush decode workers, and publish the final transcript."""

        with self._lock:
            if self._starting and not self._recording:
                # A stop arrived while startup is still in flight (mic validation
                # or stream build).  Signal cancellation and let _run_start_session
                # clean up its own handles.
                self._cancel_start.set()
                self._stop_vad.set()
                self._logger.info("stop requested while recording startup is still in progress")
                return
            if not self._recording or self._transcribing:
                self._logger.info("stop ignored because the daemon is not recording")
                return
            # Flip flags and grab the stream reference under the lock so
            # _run_start_session's final _recording assignment sees a consistent view.
            self._recording = False
            self._transcribing = True
            # _write_state calls the event sink via GLib.idle_add so holding
            # the RLock here is safe — no re-entrant lock acquisition occurs.
            self._write_state(STATE_TRANSCRIBING)
            stream = self._handles.stream
            self._handles.stream = None

        self._close_stream(stream)
        self._stop_vad.set()

        # Wait for VAD first (it enqueues None to signal decode when done),
        # then wait for decode to drain the utterance queue fully. Both joins
        # are bounded so a wedged decode worker (e.g. a CTranslate2 / OpenMP
        # internal deadlock under SIGTERM races) cannot leave the daemon
        # stuck in STATE_TRANSCRIBING — every subsequent Start/Stop/Toggle
        # would be silently rejected.
        join_timeout = 30.0
        self._join_worker(self._handles.vad_thread, "vad", timeout=join_timeout, require_exit=False)
        self._join_worker(self._handles.decode_thread, "decode", timeout=join_timeout, require_exit=False)

        vad_thread = self._handles.vad_thread
        decode_thread = self._handles.decode_thread
        vad_alive = vad_thread is not None and vad_thread.is_alive()
        decode_alive = decode_thread is not None and decode_thread.is_alive()
        self._handles.vad_thread = None
        self._handles.decode_thread = None

        if vad_alive or decode_alive:
            stuck = []
            if vad_alive:
                stuck.append("vad")
            if decode_alive:
                stuck.append("decode")
            self._emit_error(
                "worker_join_timeout",
                f"Worker(s) did not exit within {int(join_timeout)}s: {', '.join(stuck)}",
            )

            # Rotate session primitives so the leaked worker(s) cannot
            # interfere with a future session. Each leaked worker still
            # holds a reference to the OLD audio_queue, utterance_queue,
            # and stop_event; replace the daemon's references with fresh
            # objects so the next _run_start_session reads/writes a
            # different set. The leaked workers will eventually exit
            # (they see their OLD self._stop_vad set above) and be reaped
            # on process exit.
            #
            # Without this rotation, _run_start_session's clear of
            # self._stop_vad would also clear the leaked worker's stop
            # condition, allowing it to wake back up and consume audio
            # from — or post utterances into — a different session.
            #
            # Bumping _session_generation makes the bail-out explicit:
            # _decode_worker captures the generation at start and refuses
            # to publish into a session whose generation has moved on.
            # The VAD worker is already isolated because VADSegmenter
            # captures its queue references at construction time, but
            # _decode_worker rereads self._utterance_queue each iteration
            # — so without the generation check it would happily consume
            # from the rotated NEW queue.
            with self._lock:
                self._audio_queue = queue.Queue(maxsize=AUDIO_QUEUE_MAXSIZE)
                self._utterance_queue = queue.Queue(maxsize=UTTERANCE_QUEUE_MAXSIZE)
                self._stop_vad = threading.Event()
                self._session_generation += 1

            self._write_state(STATE_ERROR)
            # Force back to IDLE so the daemon does not stay wedged.
            # The leaked worker(s) are still daemon=True, so process
            # exit will eventually clean them up.
            self._write_state(STATE_IDLE)
            with self._lock:
                self._transcribing = False
            self._pending_start.clear()
            return

        final_text = self._finalize_text()
        self._write_state(STATE_IDLE)
        with self._lock:
            self._transcribing = False

        if not final_text:
            self._logger.info("no speech detected")

        # If a start request arrived while transcription was draining, honour it now.
        if self._pending_start.is_set():
            self._pending_start.clear()
            self.request_start()

    def request_start(self) -> None:
        """Start dictation asynchronously via the control-plane thread."""

        if self._shutting_down.is_set():
            return
        self._control_queue.put(self._run_start_session)

    def request_stop(self) -> None:
        """Stop dictation asynchronously via the control-plane thread.

        The cancellation flag is set synchronously (not via the queue) so
        a Stop arriving while _run_start_session is blocked inside
        input_device_resolver(), stream construction, or stream.start()
        can cancel the start at its next checkpoint. The serialized
        control worker would otherwise queue _run_stop_session behind the
        active start task, missing every cancellation window — leaving
        the daemon stuck in `starting` until the blocked call eventually
        returned.
        """

        with self._lock:
            if self._starting and not self._recording:
                self._cancel_start.set()
                self._stop_vad.set()
        if self._shutting_down.is_set():
            return
        self._control_queue.put(self._run_stop_session)

    def toggle(self) -> None:
        """Toggle between recording and idle states."""

        with self._lock:
            starting = self._starting
            state = self._state
        if starting:
            self.request_stop()
            return
        if state == STATE_RECORDING:
            self.request_stop()
            return
        if state == STATE_TRANSCRIBING:
            self._pending_start.set()
            self._logger.info("toggle deferred until transcribe completes")
            return
        self.request_start()

    def get_state(self) -> str:
        """Return the current daemon state."""

        with self._lock:
            return self._state

    def get_last_text(self) -> str:
        """Return the latest finalized transcript."""

        with self._lock:
            return self._last_text

    def ping(self) -> str:
        """Return a liveness marker for D-Bus callers."""

        return "pong"

    def shutdown(self) -> None:
        """Stop background workers and reset the daemon to idle."""

        # Set _shutting_down FIRST so request_start / request_stop become
        # no-ops immediately. Without this fence, a request_*() arriving
        # during shutdown would still enqueue work onto _control_queue
        # which the dying control thread would happily run, racing with
        # the shutdown teardown and potentially reopening the stream.
        self._shutting_down.set()

        self._pending_start.clear()
        self._cancel_start.set()
        self._stop_vad.set()
        with self._lock:
            self._recording = False
            self._starting = False
            self._transcribing = False
        self._close_stream(self._handles.stream)
        try:
            self._join_worker(self._handles.vad_thread, "vad", timeout=5.0, require_exit=False)
            self._join_worker(self._handles.decode_thread, "decode", timeout=5.0, require_exit=False)
        except _WorkerJoinTimeoutError:
            pass

        # Drain any pending control tasks BEFORE the sentinel so a
        # queued request_start() that arrived just before the fence does
        # not still execute during shutdown. New request_*() calls are
        # already filtered out by the _shutting_down check above.
        while True:
            try:
                self._control_queue.get_nowait()
            except queue.Empty:
                break

        # Stop the control-plane thread last so any in-flight task can
        # finish observing the cleared _recording / _starting flags above.
        self._control_queue.put(None)
        self._control_thread.join(timeout=5.0)
        self._write_state(STATE_IDLE)


def _load_model_and_config(argv: list[str] | None = None) -> tuple[DictationConfig, Any, dict[str, Any]]:
    """Parse CLI arguments and construct the transcription model."""

    namespace = parse_args(argv)
    config = DictationConfig.from_namespace(namespace)
    model, runtime = load_model(config)
    return config, model, runtime


def main(argv: list[str] | None = None) -> int:
    """Run the daemon as a long-lived GLib main loop process."""

    # Single base logger owns the FileHandler for daemon.log; the per-
    # subsystem loggers are children that propagate up. Python's logging
    # module gives every FileHandler its own lock, so attaching multiple
    # FileHandler instances to the same path (one per subsystem) races
    # and produces interleaved/garbled output. Funnel everything through
    # one handler instead.
    #
    # Base logger name is "kdictate.daemon" rather than "kdictate" so
    # sibling subtrees like "kdictate.ibus" (IBus engine, separate
    # process) and "kdictate.tests" (unit test loggers) do not share
    # this FileHandler via the root-level "kdictate" ancestor. Codex
    # flagged on PR #6 that the broader name made daemon.log the sink
    # for the entire kdictate.* hierarchy, which is how the test-leak
    # bug fixed in b1cc382 was able to happen in the first place.
    base_logger = configure_logging("kdictate.daemon", log_file="daemon.log")
    logger = get_propagating_child(base_logger, "core")
    try:
        config, model, runtime = _load_model_and_config(argv)
    except (ConfigurationError, FileNotFoundError) as exc:
        logger.error("%s", exc)
        return 1

    backend_name = config.backend
    need_cpu_model = backend_name == "cpu"

    if backend_name in ("gpu", "auto"):
        from kdictate.backend import create_gpu_backend
        gpu = create_gpu_backend(config)
        if gpu is not None:
            backend: TranscriptionBackend = gpu
            logger.info("using GPU backend (whisper.cpp + Vulkan)")
        elif backend_name == "gpu":
            logger.error("GPU backend requested but unavailable")
            return 1
        else:
            need_cpu_model = True

    if need_cpu_model:
        logger.info(
            "CPU backend: device=%s compute_type=%s cpu_threads=%s",
            runtime["device"],
            runtime["compute_type"],
            runtime["cpu_threads"],
        )
        backend = create_cpu_backend(model, config)
        logger.info("using CPU backend (faster-whisper)")

    daemon = DictationDaemon(config, backend, config.runtime_paths, logger=logger)

    try:
        from kdictate.service.dbus_service import SessionDbusService
    except Exception as exc:  # noqa: BLE001
        logger.error("failed to import session service: %s", exc)
        return 1

    service = SessionDbusService(daemon, logger=get_propagating_child(base_logger, "dbus"))

    try:
        service.start()
    except Exception as exc:  # noqa: BLE001
        logger.error("failed to start D-Bus service: %s", exc)
        daemon.shutdown()
        return 1
    daemon.set_event_sink(service)

    # KWin Wayland sends Ctrl+Space to whoever owns the screen-reader
    # KeyboardMonitor name. kglobalaccel/.desktop registration alone leaves
    # the shortcut inactive on a fresh install, so claim that name here and
    # forward releases straight into the daemon's toggle.
    from kdictate.core.kwin_hotkey import KwinHotkeyListener

    hotkey_listener: KwinHotkeyListener | None = KwinHotkeyListener(
        on_activate=daemon.toggle,
        logger=get_propagating_child(base_logger, "hotkey"),
    )
    try:
        hotkey_listener.start()
    except Exception as exc:  # noqa: BLE001
        # start() may have partially succeeded — e.g. RequestName claimed
        # the Orca screen-reader name but SetKeyGrabs then failed because
        # we are not on a kwin session. Drop everything we did claim so
        # the leaked Orca name doesn't block assistive tooling for the
        # rest of the daemon's lifetime.
        try:
            hotkey_listener.stop()
        except Exception as cleanup_exc:  # noqa: BLE001
            logger.warning(
                "KWin hotkey listener cleanup after failed start raised: %s",
                cleanup_exc,
            )
        logger.warning("KWin hotkey listener disabled: %s", exc)
        hotkey_listener = None

    loop = GLib.MainLoop()

    def _on_sigterm() -> bool:
        loop.quit()
        return GLib.SOURCE_REMOVE

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, _on_sigterm)
    try:
        logger.info("daemon started")
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        if hotkey_listener is not None:
            try:
                hotkey_listener.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("KWin hotkey listener stop failed: %s", exc)
        daemon.shutdown()
        service.stop()

    return 0
