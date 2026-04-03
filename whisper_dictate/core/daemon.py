"""Core dictation daemon logic and process entrypoint."""

from __future__ import annotations

import logging
import queue
import signal
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib

from whisper_dictate.config import DictationConfig, parse_args
from whisper_dictate.constants import STATE_ERROR, STATE_IDLE, STATE_RECORDING, STATE_STARTING, STATE_TRANSCRIBING
from whisper_dictate.core.audio import resolve_default_input_device
from whisper_dictate.exceptions import AudioInputError, ConfigurationError, TranscriptionError
from whisper_dictate.logging_utils import configure_logging
from whisper_dictate.runtime import RuntimePaths, write_last_text, write_state
from whisper_common import (
    AUDIO_QUEUE_MAXSIZE,
    UTTERANCE_QUEUE_MAXSIZE,
    VADConfig,
    VADSegmenter,
    load_whisper_model,
    transcribe_pcm,
)
from runtime_profile import resolve_runtime, set_thread_env


DEFAULT_MODEL_DIR = Path(__file__).resolve().parent.parent.parent / "models/whisper-large-v3-turbo-ct2"


class DaemonEventSink(Protocol):
    """Observer interface used to publish daemon events to a transport layer."""

    def state_changed(self, state: str) -> None: ...

    def partial_transcript(self, text: str) -> None: ...

    def final_transcript(self, text: str) -> None: ...

    def error_occurred(self, code: str, message: str) -> None: ...


class _NullEventSink:
    """No-op sink used when the daemon runs without an attached transport."""

    def state_changed(self, state: str) -> None:
        del state

    def partial_transcript(self, text: str) -> None:
        del text

    def final_transcript(self, text: str) -> None:
        del text

    def error_occurred(self, code: str, message: str) -> None:
        del code, message


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

    logging.getLogger("whisper_dictate.core").info("loading model from %s", config.model_dir)
    model = load_whisper_model(
        config.model_dir,
        device=runtime["device"],
        compute_type=runtime["compute_type"],
        cpu_threads=runtime["cpu_threads"],
        num_workers=2,
    )
    return model, runtime


class DictationDaemon:
    """Own microphone capture, VAD segmentation, transcription, and state."""

    def __init__(
        self,
        config: DictationConfig,
        model: Any,
        runtime_paths: RuntimePaths,
        *,
        event_sink: DaemonEventSink | None = None,
        logger: logging.Logger | None = None,
        stream_factory: Callable[..., Any] | None = None,
        input_device_resolver: Callable[[], tuple[str, bool]] = resolve_default_input_device,
        transcription_fn: Callable[..., str] = transcribe_pcm,
    ) -> None:
        self.config = config
        self.model = model
        self.runtime_paths = runtime_paths
        self._event_sink = event_sink or _NullEventSink()
        self._logger = logger or configure_logging("whisper_dictate.core")
        self._stream_factory = stream_factory
        self._input_device_resolver = input_device_resolver
        self._transcription_fn = transcription_fn
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
        self._handles = _ThreadHandles()

        self._write_state(STATE_IDLE)
        write_last_text(self.runtime_paths.last_text_file, "")

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

    def _emit_error(self, code: str, message: str) -> None:
        """Publish a structured failure event."""

        self._logger.error("%s: %s", code, message)
        self._event_sink.error_occurred(code, message)

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
        self._logger.info("partial transcript emitted: %s", cumulative)
        self._event_sink.partial_transcript(cumulative)

    def _finalize_text(self) -> str:
        """Persist and publish the current full transcript."""

        with self._lock:
            text = " ".join(self._streamed_text).strip()
            self._last_text = text
        write_last_text(self.runtime_paths.last_text_file, text)
        self._logger.info("final transcript emitted: %s", text)
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

        while True:
            item = self._utterance_queue.get()
            if item is None:
                break
            pcm_chunks, _audio_seconds = item
            try:
                text = self._transcription_fn(
                    self.model,
                    pcm_chunks,
                    language=self.config.language,
                    beam_size=self.config.beam_size,
                    no_speech_threshold=self.config.no_speech_threshold,
                    condition_on_previous_text=self.config.condition_on_previous_text,
                    vad_filter=self.config.vad_filter,
                )
                if text:
                    self._record_partial_text(text)
            except Exception as exc:  # noqa: BLE001
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
                self._pending_start.set()
                self._logger.info("start deferred until transcription completes")
                return
            self._starting = True
            self._transcribing = False
            self._cancel_start.clear()
            self._reset_session_buffers()
            self._handles = _ThreadHandles()
            self._stop_vad.clear()

        self._write_state(STATE_STARTING)
        mic_name, mic_usable = self._input_device_resolver()
        if not mic_usable:
            with self._lock:
                self._starting = False
                self._cancel_start.clear()
            error = AudioInputError(f"No usable input device: {mic_name}")
            self._write_state(STATE_ERROR)
            self._emit_error("audio_input_unavailable", str(error))
            self._write_state(STATE_IDLE)
            return
        if self._cancel_start.is_set():
            with self._lock:
                self._starting = False
                self._cancel_start.clear()
            self._logger.info("recording start cancelled before activation")
            self._write_state(STATE_IDLE)
            return

        self._handles.decode_thread = threading.Thread(
            target=self._decode_worker,
            name="whisper-dictate-decode",
            daemon=True,
        )
        self._handles.vad_thread = threading.Thread(
            target=self._vad_worker,
            name="whisper-dictate-vad",
            daemon=True,
        )
        self._handles.decode_thread.start()
        self._handles.vad_thread.start()
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
            if self._cancel_start.is_set():
                self._cleanup_start_handles()
                with self._lock:
                    self._starting = False
                    self._cancel_start.clear()
                self._logger.info("recording start cancelled before activation")
                self._write_state(STATE_IDLE)
                return
            stream.start()
            if self._cancel_start.is_set():
                self._cleanup_start_handles()
                with self._lock:
                    self._starting = False
                    self._cancel_start.clear()
                self._logger.info("recording start cancelled before activation")
                self._write_state(STATE_IDLE)
                return
            with self._lock:
                self._recording = True
                self._starting = False
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
                self._cancel_start.set()
                self._stop_vad.set()
                self._logger.info("stop requested while recording startup is still in progress")
                return
            if not self._recording or self._transcribing:
                self._logger.info("stop ignored because the daemon is not recording")
                return
            self._recording = False
            self._transcribing = True
            self._write_state(STATE_TRANSCRIBING)
            stream = self._handles.stream
            self._handles.stream = None

        self._close_stream(stream)
        self._stop_vad.set()

        self._join_worker(self._handles.vad_thread, "vad", timeout=None, require_exit=True)
        self._join_worker(self._handles.decode_thread, "decode", timeout=None, require_exit=True)
        self._handles.vad_thread = None
        self._handles.decode_thread = None

        final_text = self._finalize_text()
        self._write_state(STATE_IDLE)
        with self._lock:
            self._transcribing = False

        if not final_text:
            self._logger.info("no speech detected")

        if self._pending_start.is_set():
            self._pending_start.clear()
            self.request_start()

    def request_start(self) -> None:
        """Start dictation asynchronously."""

        threading.Thread(target=self._run_start_session, name="whisper-dictate-start", daemon=True).start()

    def request_stop(self) -> None:
        """Stop dictation asynchronously."""

        threading.Thread(target=self._run_stop_session, name="whisper-dictate-stop", daemon=True).start()

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
        self._write_state(STATE_IDLE)


def _load_model_and_config(argv: list[str] | None = None) -> tuple[DictationConfig, Any, dict[str, Any]]:
    """Parse CLI arguments and construct the transcription model."""

    namespace = parse_args(argv)
    config = DictationConfig.from_namespace(namespace)
    model, runtime = load_model(config)
    return config, model, runtime


def main(argv: list[str] | None = None) -> int:
    """Run the daemon as a long-lived GLib main loop process."""

    logger = configure_logging("whisper_dictate.core")
    try:
        config, model, runtime = _load_model_and_config(argv)
    except (ConfigurationError, FileNotFoundError) as exc:
        logger.error("%s", exc)
        return 1

    logger.info(
        "model ready device=%s compute_type=%s cpu_threads=%s",
        runtime["device"],
        runtime["compute_type"],
        runtime["cpu_threads"],
    )

    daemon = DictationDaemon(config, model, config.runtime_paths, logger=logger)

    try:
        from whisper_dictate.service.dbus_service import SessionDbusService
    except Exception as exc:  # noqa: BLE001
        logger.error("failed to import session service: %s", exc)
        return 1

    service = SessionDbusService(daemon, logger=configure_logging("whisper_dictate.dbus"))

    try:
        service.start()
    except Exception as exc:  # noqa: BLE001
        logger.error("failed to start D-Bus service: %s", exc)
        daemon.shutdown()
        return 1
    daemon.set_event_sink(service)

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
        daemon.shutdown()
        service.stop()

    return 0
