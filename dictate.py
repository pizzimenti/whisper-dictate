"""Long-lived Whisper dictation daemon.

The daemon keeps the speech model warm in memory and exposes a tiny signal-based
control plane:

- ``SIGUSR1`` starts recording.
- ``SIGUSR2`` stops recording and transcribes the captured audio.

Runtime state is persisted under ``XDG_RUNTIME_DIR`` so terminal helpers and the
Wayland hotkey listener can coordinate without importing audio or GI bindings.
Typing can happen here or be delegated to the hotkey listener with
``--no-type-output``.

During a PTT session, audio is segmented and decoded in real-time (each utterance
committed by a silence gap or max-length limit), but text is typed in full only
after the key is released to avoid modifier-key interference.
"""

from __future__ import annotations

import argparse
import queue
import signal
import sys
import threading
from pathlib import Path
from typing import Any

import subprocess

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib

from desktop_actions import DictationNotifier, notify, type_text
from dictate_runtime import (
    STATE_IDLE,
    STATE_RECORDING,
    STATE_TRANSCRIBING,
    RuntimePaths,
    default_runtime_paths,
    write_last_text,
    write_state,
)
from runtime_profile import recommended_shortform_cpu_threads, resolve_runtime, set_thread_env
from whisper_common import VADConfig, VADSegmenter, load_whisper_model, transcribe_pcm


DEFAULT_RUNTIME_PATHS = default_runtime_paths()
DEFAULT_MODEL_DIR = Path(__file__).parent / "models/whisper-large-v3-turbo-ct2"


def _get_default_input_device() -> tuple[str, bool]:
    """Return (device_description, is_usable) for the default PipeWire/PulseAudio source.

    A source is considered unusable if it is a monitor (output loopback) or if
    no default source is configured.  The description is a human-friendly name
    suitable for display in a notification.
    """
    try:
        result = subprocess.run(
            ["pactl", "get-default-source"],
            capture_output=True, text=True, timeout=3,
        )
        source_name = result.stdout.strip()
    except Exception:  # noqa: BLE001
        return ("unknown", False)

    if not source_name:
        return ("none", False)

    if source_name.endswith(".monitor"):
        return (source_name, False)

    # Ask pactl for the human-readable description.
    try:
        result = subprocess.run(
            ["pactl", "list", "sources"],
            capture_output=True, text=True, timeout=3,
        )
        in_target = False
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("Name:") and stripped.split(None, 1)[1] == source_name:
                in_target = True
            elif in_target and stripped.startswith("Description:"):
                return (stripped.split(":", 1)[1].strip(), True)
    except Exception:  # noqa: BLE001
        pass

    return (source_name, True)


def parse_args() -> argparse.Namespace:
    """Parse daemon configuration for model/runtime behavior."""

    parser = argparse.ArgumentParser(
        description="Whisper-Dictate daemon. SIGUSR1 starts recording, SIGUSR2 stops."
    )
    parser.add_argument(
        "--model-dir",
        default=str(DEFAULT_MODEL_DIR),
        help="Path to the CTranslate2 model directory.",
    )
    parser.add_argument("--language", default="en", help="Language code for transcription.")
    parser.add_argument("--sample-rate", type=int, default=16000, help="Microphone sample rate.")
    parser.add_argument("--beam-size", type=int, default=1, help="Whisper beam size.")
    parser.add_argument(
        "--condition-on-previous-text",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Condition on previous text between segments.",
    )
    parser.add_argument(
        "--vad-filter",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Enable built-in VAD filtering before decode.",
    )
    parser.add_argument(
        "--no-speech-threshold",
        type=float,
        default=0.6,
        help="Reject segments below this no-speech confidence threshold.",
    )
    parser.add_argument(
        "--cpu-threads",
        type=int,
        default=recommended_shortform_cpu_threads(),
        help="Override CPU thread count. Defaults to a short-form latency-oriented value.",
    )
    parser.add_argument(
        "--compute-type",
        default="int8",
        choices=("float32", "float16", "int8", "int8_float16"),
        help="Compute type. Defaults to int8 for the tuned CPU dictation path.",
    )
    parser.add_argument(
        "--state-file",
        default=str(DEFAULT_RUNTIME_PATHS.state_file),
        help="Path to the daemon state file used by control helpers.",
    )
    parser.add_argument(
        "--last-text-file",
        default=str(DEFAULT_RUNTIME_PATHS.last_text_file),
        help="Path to the latest transcript file used by control helpers.",
    )
    parser.add_argument(
        "--type-output",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Type the transcript into the focused window. Disable when an external helper owns typing.",
    )
    # VAD / streaming parameters (same defaults as mic_realtime.py)
    parser.add_argument(
        "--block-ms",
        type=int,
        default=30,
        help="Audio capture block duration in milliseconds.",
    )
    parser.add_argument(
        "--energy-threshold",
        type=float,
        default=300.0,
        help="RMS threshold for speech detection. Increase to ignore noise.",
    )
    parser.add_argument(
        "--silence-ms",
        type=int,
        default=220,
        help="Silence duration (ms) that commits the current utterance for transcription.",
    )
    parser.add_argument(
        "--min-speech-ms",
        type=int,
        default=180,
        help="Minimum speech duration (ms) required to transcribe an utterance.",
    )
    parser.add_argument(
        "--start-speech-ms",
        type=int,
        default=90,
        help="Consecutive voiced duration (ms) required before an utterance starts.",
    )
    parser.add_argument(
        "--max-utterance-s",
        type=float,
        default=2.5,
        help="Force-commit an utterance when it reaches this length in seconds.",
    )
    return parser.parse_args()


def _load_model(args: argparse.Namespace) -> tuple[Any, dict[str, Any]]:
    """Load the configured faster-whisper model and runtime profile."""

    runtime = resolve_runtime("cpu", args.compute_type, args.cpu_threads)
    set_thread_env(runtime["cpu_threads"])

    model_dir = Path(args.model_dir)
    if not model_dir.exists():
        raise FileNotFoundError(f"Model directory not found: {model_dir}")

    print(f"Loading model from {model_dir}...", flush=True)
    model = load_whisper_model(
        model_dir,
        device=runtime["device"],
        compute_type=runtime["compute_type"],
        cpu_threads=runtime["cpu_threads"],
        num_workers=2,
    )
    return model, runtime


def _transcribe_pcm_with_args(model: Any, pcm_chunks: list[Any], args: argparse.Namespace) -> str:
    """Transcribe a list of int16 PCM chunks using daemon CLI args."""
    return transcribe_pcm(
        model,
        pcm_chunks,
        language=args.language,
        beam_size=args.beam_size,
        no_speech_threshold=args.no_speech_threshold,
        condition_on_previous_text=args.condition_on_previous_text,
        vad_filter=args.vad_filter,
    )


class DictationDaemon:
    """Own the warm model plus the record/transcribe lifecycle."""

    def __init__(self, args: argparse.Namespace, model: Any, runtime_paths: RuntimePaths) -> None:
        self.args = args
        self.model = model
        self.runtime_paths = runtime_paths
        self._notifier = DictationNotifier()

        # Lock protects: _recording, _transcribing, _stream, _streamed_text,
        # _vad_thread, _decode_thread.  Hold briefly — never call blocking I/O
        # (join, stream.start, transcribe) while holding the lock.
        self._lock = threading.Lock()
        self._recording = False
        self._transcribing = False
        self._stream: Any | None = None

        # Streaming pipeline state (queues and event are thread-safe on their own)
        self._audio_queue: queue.Queue = queue.Queue(maxsize=512)
        self._utterance_queue: queue.Queue = queue.Queue(maxsize=64)
        self._stop_vad = threading.Event()
        self._pending_start = threading.Event()
        self._vad_thread: threading.Thread | None = None
        self._decode_thread: threading.Thread | None = None
        self._streamed_text: list[str] = []

        self._set_runtime_state(STATE_IDLE)
        write_last_text(self.runtime_paths.last_text_file, "")

    def _set_runtime_state(self, value: str) -> None:
        write_state(self.runtime_paths.state_file, value)

    def _close_stream(self, stream: Any | None) -> None:
        """Stop and close a sounddevice stream if one is present."""

        if stream is None:
            return
        try:
            stream.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass

    def _input_callback(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        del frames, time_info, status
        with self._lock:
            if not self._recording:
                return
        chunk = indata[:, 0].copy()
        try:
            self._audio_queue.put_nowait(chunk)
        except queue.Full:
            pass  # drop block rather than stall the audio thread

    def _vad_worker(self) -> None:
        """Segment audio by silence/maxlen and post utterance chunks to the decode queue."""
        vad = VADSegmenter(
            config=VADConfig(
                sample_rate=self.args.sample_rate,
                block_ms=self.args.block_ms,
                energy_threshold=self.args.energy_threshold,
                silence_ms=self.args.silence_ms,
                min_speech_ms=self.args.min_speech_ms,
                start_speech_ms=self.args.start_speech_ms,
                max_utterance_s=self.args.max_utterance_s,
            ),
            audio_queue=self._audio_queue,
            utterance_queue=self._utterance_queue,
            stop_event=self._stop_vad,
        )
        vad.run()

    def _decode_worker(self) -> None:
        """Transcribe each utterance chunk, accumulating text for final typing."""
        while True:
            item = self._utterance_queue.get()
            if item is None:
                break
            pcm_chunks, _audio_seconds = item
            try:
                text = _transcribe_pcm_with_args(self.model, pcm_chunks, self.args)
                if text:
                    with self._lock:
                        self._streamed_text.append(text)
                    print(f"Streamed: {text}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"Decode failed: {exc}", file=sys.stderr, flush=True)

    def start_recording(self) -> None:
        """Start the microphone stream and streaming VAD/decode pipeline."""

        import sounddevice as sd

        mic_name, mic_usable = _get_default_input_device()
        if not mic_usable:
            notify(f"no usable microphone ({mic_name})")
            print(f"No usable input device: {mic_name}", file=sys.stderr, flush=True)
            return

        with self._lock:
            if self._recording:
                return
            if self._transcribing:
                return
            self._recording = True
            self._pending_start.clear()
            self._streamed_text = []
            # Drain any stale audio from a previous session
            while not self._audio_queue.empty():
                self._audio_queue.get_nowait()
            self._stop_vad.clear()
            self._set_runtime_state(STATE_RECORDING)
            write_last_text(self.runtime_paths.last_text_file, "")

            # Create and start threads while holding the lock so that
            # stop_and_transcribe always sees them if _recording was True.
            self._decode_thread = threading.Thread(target=self._decode_worker, daemon=True)
            self._decode_thread.start()
            self._vad_thread = threading.Thread(target=self._vad_worker, daemon=True)
            self._vad_thread.start()

        block_size = max(1, int(self.args.sample_rate * self.args.block_ms / 1000))
        stream: Any | None = None
        try:
            stream = sd.InputStream(
                samplerate=self.args.sample_rate,
                channels=1,
                dtype="int16",
                blocksize=block_size,
                callback=self._input_callback,
            )
            with self._lock:
                self._stream = stream
            stream.start()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._recording = False
                self._stream = None
                self._set_runtime_state(STATE_IDLE)
            # Signal VAD to stop; it will post None to the decode queue
            self._stop_vad.set()
            if self._vad_thread is not None:
                self._vad_thread.join(timeout=5)
            if self._decode_thread is not None:
                self._decode_thread.join(timeout=5)
            self._close_stream(stream)
            notify("Microphone start failed.")
            print(f"Recording start failed: {exc}", file=sys.stderr, flush=True)

            return

        print(f"Recording started (streaming) — mic: {mic_name}", flush=True)
        self._notifier.started(mic_name)

    def stop_and_transcribe(self) -> None:
        """Stop recording, flush the streaming pipeline, and finalize."""

        with self._lock:
            if not self._recording:
                return
            self._recording = False
            self._transcribing = True
            stream = self._stream
            self._stream = None
            self._set_runtime_state(STATE_TRANSCRIBING)

        self._notifier.transcribing()
        self._close_stream(stream)

        # Type whatever is already decoded — key is released so Ctrl is no longer held.
        with self._lock:
            already_typed_count = len(self._streamed_text)
            already_decoded = " ".join(self._streamed_text).strip()

        if already_decoded and self.args.type_output:
            type_text(already_decoded)

        # Signal VAD to flush remaining buffer and stop; it posts None to decode queue.
        self._stop_vad.set()

        if self._vad_thread is not None:
            self._vad_thread.join(timeout=10)
            self._vad_thread = None

        if self._decode_thread is not None:
            self._decode_thread.join(timeout=10)
            self._decode_thread = None

        with self._lock:
            new_parts = self._streamed_text[already_typed_count:]
            text = " ".join(self._streamed_text).strip()
            self._transcribing = False
            self._set_runtime_state(STATE_IDLE)

        if new_parts:
            new_text = " ".join(new_parts).strip()
            if new_text and self.args.type_output:
                type_text((" " if already_decoded else "") + new_text)

        if text:
            write_last_text(self.runtime_paths.last_text_file, text)
            print(f"Done: {text}", flush=True)
        else:
            write_last_text(self.runtime_paths.last_text_file, "")
            print("No speech detected.", flush=True)
        self._notifier.stopped()

        if self._pending_start.is_set():
            self._pending_start.clear()
            self.request_start()

    @property
    def state(self) -> str:
        """Current daemon state, safe to call from any thread."""
        with self._lock:
            if self._recording:
                return STATE_RECORDING
            if self._transcribing:
                return STATE_TRANSCRIBING
            return STATE_IDLE

    def request_start(self) -> None:
        """Queue a non-blocking start request (signal handler or hotkey safe)."""

        threading.Thread(target=self.start_recording, daemon=True).start()

    def request_stop(self) -> None:
        """Queue a non-blocking stop/transcribe request (signal handler or hotkey safe)."""

        threading.Thread(target=self.stop_and_transcribe, daemon=True).start()

    def shutdown(self) -> None:
        """Reset the daemon to idle and close any open microphone stream."""

        with self._lock:
            self._recording = False
            self._transcribing = False
            stream = self._stream
            self._stream = None

        self._close_stream(stream)
        self._stop_vad.set()
        self._set_runtime_state(STATE_IDLE)


def main() -> int:
    """Load the model, install signal handlers, and stay resident."""

    args = parse_args()

    try:
        model, runtime = _load_model(args)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        print("Run: python prepare_model.py", file=sys.stderr)
        return 1

    print(
        f"Model ready. device={runtime['device']} compute_type={runtime['compute_type']} "
        f"cpu_threads={runtime['cpu_threads']}",
        flush=True,
    )
    notify("dictation ready")

    daemon = DictationDaemon(
        args=args,
        model=model,
        runtime_paths=RuntimePaths(
            state_file=Path(args.state_file),
            last_text_file=Path(args.last_text_file),
        ),
    )

    loop = GLib.MainLoop()

    def _on_sigusr1() -> bool:
        daemon.request_start()
        return GLib.SOURCE_CONTINUE

    def _on_sigusr2() -> bool:
        daemon.request_stop()
        return GLib.SOURCE_CONTINUE

    def _on_sigterm() -> bool:
        loop.quit()
        return GLib.SOURCE_REMOVE

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, _on_sigusr1)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR2, _on_sigusr2)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, _on_sigterm)

    from kglobal_hotkey import HotkeyListener

    try:
        listener = HotkeyListener(daemon)
        listener.register()
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: hotkey listener unavailable: {exc}", file=sys.stderr)
        print("  Terminal control still available via dictatectl.py", file=sys.stderr)

    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        daemon.shutdown()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
