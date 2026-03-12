"""Long-lived Whisper dictation daemon.

The daemon keeps the speech model warm in memory and exposes a tiny signal-based
control plane:

- ``SIGUSR1`` starts recording.
- ``SIGUSR2`` stops recording and transcribes the captured audio.

Runtime state is persisted under ``XDG_RUNTIME_DIR`` so terminal helpers and the
Wayland hotkey listener can coordinate without importing audio or GI bindings.
Typing can happen here or be delegated to the hotkey listener with
``--no-type-output``.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

from desktop_actions import notify, type_text
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


DEFAULT_RUNTIME_PATHS = default_runtime_paths()
DEFAULT_MODEL_DIR = Path(__file__).parent / "models/distil-medium-en-ct2-int8"


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
    return parser.parse_args()


def _load_model(args: argparse.Namespace) -> tuple[Any, dict[str, Any]]:
    """Load the configured faster-whisper model and runtime profile."""

    runtime = resolve_runtime("cpu", args.compute_type, args.cpu_threads)
    set_thread_env(runtime["cpu_threads"])

    model_dir = Path(args.model_dir)
    if not model_dir.exists():
        raise FileNotFoundError(f"Model directory not found: {model_dir}")

    print(f"Loading model from {model_dir}...", flush=True)
    from faster_whisper import WhisperModel

    model = WhisperModel(
        str(model_dir),
        device=runtime["device"],
        compute_type=runtime["compute_type"],
        cpu_threads=runtime["cpu_threads"],
        num_workers=1,
    )
    return model, runtime


def _normalize_segments(segments: Any) -> str:
    """Collapse faster-whisper segments into one normalized transcript string."""

    text = " ".join(segment.text.strip() for segment in segments if segment.text.strip())
    return " ".join(text.split())


class DictationDaemon:
    """Own the warm model plus the record/transcribe lifecycle."""

    def __init__(self, args: argparse.Namespace, model: Any, runtime_paths: RuntimePaths) -> None:
        self.args = args
        self.model = model
        self.runtime_paths = runtime_paths
        self._lock = threading.Lock()
        self._recording = False
        self._transcribing = False
        self._chunks: list[Any] = []
        self._stream: Any | None = None

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
            if self._recording:
                self._chunks.append(indata[:, 0].copy())

    def start_recording(self) -> None:
        """Start the microphone stream unless the daemon is already busy."""

        import sounddevice as sd

        with self._lock:
            if self._recording:
                return
            if self._transcribing:
                notify("Still transcribing previous utterance.")
                return
            self._recording = True
            self._chunks = []
            self._set_runtime_state(STATE_RECORDING)
            write_last_text(self.runtime_paths.last_text_file, "")

        stream: Any | None = None
        try:
            stream = sd.InputStream(
                samplerate=self.args.sample_rate,
                channels=1,
                dtype="int16",
                callback=self._input_callback,
            )
            with self._lock:
                self._stream = stream
            stream.start()
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._recording = False
                self._stream = None
                self._chunks = []
                self._set_runtime_state(STATE_IDLE)
            self._close_stream(stream)
            notify("Microphone start failed.")
            print(f"Recording start failed: {exc}", file=sys.stderr, flush=True)
            return

        print("Recording started.", flush=True)
        notify("● Listening...")

    def stop_and_transcribe(self) -> None:
        """Stop recording, run Whisper, and persist the latest transcript."""

        import numpy as np

        with self._lock:
            if not self._recording:
                return
            self._recording = False
            self._transcribing = True
            chunks = list(self._chunks)
            stream = self._stream
            self._stream = None
            self._set_runtime_state(STATE_TRANSCRIBING)

        self._close_stream(stream)

        if not chunks:
            with self._lock:
                self._transcribing = False
                self._set_runtime_state(STATE_IDLE)
            write_last_text(self.runtime_paths.last_text_file, "")
            notify("Nothing recorded.")
            return

        print("Transcribing...", flush=True)
        notify("Transcribing...")

        try:
            audio = np.concatenate(chunks).astype(np.float32) / 32768.0
            audio = audio.clip(-1.0, 1.0)
            segments, _info = self.model.transcribe(
                audio,
                language=self.args.language,
                beam_size=self.args.beam_size,
                best_of=1,
                temperature=0.0,
                condition_on_previous_text=self.args.condition_on_previous_text,
                vad_filter=self.args.vad_filter,
                no_speech_threshold=self.args.no_speech_threshold,
                without_timestamps=True,
            )
            text = _normalize_segments(segments)

            if text:
                write_last_text(self.runtime_paths.last_text_file, text)
                if self.args.type_output:
                    type_text(text)
                preview = text[:60] + ("..." if len(text) > 60 else "")
                notify(f"✓ {preview}")
                action = "Typed" if self.args.type_output else "Captured"
                print(f"{action}: {text}", flush=True)
            else:
                write_last_text(self.runtime_paths.last_text_file, "")
                notify("No speech detected.")
                print("No speech detected.", flush=True)
        except Exception as exc:  # noqa: BLE001
            write_last_text(self.runtime_paths.last_text_file, "")
            notify("Transcription failed.")
            print(f"Transcription failed: {exc}", file=sys.stderr, flush=True)
        finally:
            with self._lock:
                self._transcribing = False
                self._set_runtime_state(STATE_IDLE)

    def request_start(self) -> None:
        """Queue a non-blocking start request from a signal handler."""

        threading.Thread(target=self.start_recording, daemon=True).start()

    def request_stop(self) -> None:
        """Queue a non-blocking stop/transcribe request from a signal handler."""

        threading.Thread(target=self.stop_and_transcribe, daemon=True).start()

    def install_signal_handlers(self) -> None:
        """Bind ``SIGUSR1``/``SIGUSR2`` to the daemon control actions."""

        def _on_sigusr1(signum: int, frame: Any) -> None:
            del signum, frame
            self.request_start()

        def _on_sigusr2(signum: int, frame: Any) -> None:
            del signum, frame
            self.request_stop()

        signal.signal(signal.SIGUSR1, _on_sigusr1)
        signal.signal(signal.SIGUSR2, _on_sigusr2)

    def shutdown(self) -> None:
        """Reset the daemon to idle and close any open microphone stream."""

        with self._lock:
            self._recording = False
            self._transcribing = False
            stream = self._stream
            self._stream = None

        self._close_stream(stream)

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
    notify("Whisper-Dictate ready")

    daemon = DictationDaemon(
        args=args,
        model=model,
        runtime_paths=RuntimePaths(
            state_file=Path(args.state_file),
            last_text_file=Path(args.last_text_file),
        ),
    )
    daemon.install_signal_handlers()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        daemon.shutdown()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
