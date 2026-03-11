from __future__ import annotations

"""Whisper-KDE system dictation daemon.

Send SIGUSR1 to start recording and SIGUSR2 to stop. On stop, the daemon
transcribes the captured audio and types the result into the focused window
via ydotool.

Intended to run as a long-lived process (e.g. systemd user service) so the
model stays loaded in memory between recordings.
"""

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from runtime_profile import recommended_shortform_cpu_threads, resolve_runtime, set_thread_env


DEFAULT_STATE_FILE = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / f"whisper-dictate-{os.getuid()}.state"


def _notify(msg: str) -> None:
    subprocess.Popen(
        ["notify-send", "-a", "whisper-dictate", "-t", "3000", msg],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _type_text(text: str) -> None:
    subprocess.run(["ydotool", "type", "--", text], check=False)


def _write_state(state_file: Path, value: str) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(f"{value}\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Whisper-KDE dictation daemon. SIGUSR1 starts recording, SIGUSR2 stops."
    )
    parser.add_argument(
        "--model-dir",
        default=str(Path(__file__).parent / "models/distil-medium-en-ct2-int8"),
        help="Path to CTranslate2 model directory.",
    )
    parser.add_argument("--language", default="en", help="Language code (e.g. en, es, fr).")
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
        default=str(DEFAULT_STATE_FILE),
        help="Path to a small runtime state file used by hotkey helper scripts.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    runtime = resolve_runtime("cpu", args.compute_type, args.cpu_threads)
    set_thread_env(runtime["cpu_threads"])

    model_dir = Path(args.model_dir)
    if not model_dir.exists():
        print(f"Model directory not found: {model_dir}", file=sys.stderr)
        print("Run: python prepare_model.py", file=sys.stderr)
        return 1

    print(f"Loading model from {model_dir}...", flush=True)
    from faster_whisper import WhisperModel

    model = WhisperModel(
        str(model_dir),
        device=runtime["device"],
        compute_type=runtime["compute_type"],
        cpu_threads=runtime["cpu_threads"],
        num_workers=1,
    )
    print(
        f"Model ready. device={runtime['device']} compute_type={runtime['compute_type']} "
        f"cpu_threads={runtime['cpu_threads']}",
        flush=True,
    )
    _notify("Whisper-KDE ready")

    state_file = Path(args.state_file)
    _lock = threading.Lock()
    _state: dict = {"recording": False, "transcribing": False, "chunks": [], "stream": None}
    _write_state(state_file, "idle")

    def _set_runtime_state(value: str) -> None:
        _write_state(state_file, value)

    def _start_recording() -> None:
        import sounddevice as sd

        with _lock:
            if _state["recording"]:
                return
            if _state["transcribing"]:
                _notify("Still transcribing previous utterance.")
                return
            _state["recording"] = True
            _state["chunks"] = []
            _set_runtime_state("recording")

        def _cb(indata, frames, t, status):
            with _lock:
                if _state["recording"]:
                    _state["chunks"].append(indata[:, 0].copy())

        stream = sd.InputStream(
            samplerate=args.sample_rate,
            channels=1,
            dtype="int16",
            callback=_cb,
        )
        with _lock:
            _state["stream"] = stream
        stream.start()
        print("Recording started.", flush=True)
        _notify("● Listening...")

    def _stop_and_transcribe() -> None:
        import numpy as np

        with _lock:
            if not _state["recording"]:
                return
            _state["recording"] = False
            _state["transcribing"] = True
            chunks = list(_state["chunks"])
            stream = _state["stream"]
            _state["stream"] = None
            _set_runtime_state("transcribing")

        if stream is not None:
            stream.stop()
            stream.close()

        if not chunks:
            with _lock:
                _state["transcribing"] = False
                _set_runtime_state("idle")
            _notify("Nothing recorded.")
            return

        print("Transcribing...", flush=True)
        _notify("Transcribing...")

        try:
            audio = np.concatenate(chunks).astype(np.float32) / 32768.0
            audio = audio.clip(-1.0, 1.0)
            segments, _ = model.transcribe(
                audio,
                language=args.language,
                beam_size=args.beam_size,
                best_of=1,
                temperature=0.0,
                condition_on_previous_text=args.condition_on_previous_text,
                vad_filter=args.vad_filter,
                no_speech_threshold=args.no_speech_threshold,
                without_timestamps=True,
            )
            text = " ".join(s.text.strip() for s in segments if s.text.strip())
            text = " ".join(text.split())

            if text:
                _type_text(text)
                preview = text[:60] + ("..." if len(text) > 60 else "")
                _notify(f"✓ {preview}")
                print(f"Typed: {text}", flush=True)
            else:
                _notify("No speech detected.")
                print("No speech detected.", flush=True)
        except Exception as exc:  # noqa: BLE001
            _notify("Transcription failed.")
            print(f"Transcription failed: {exc}", file=sys.stderr, flush=True)
        finally:
            with _lock:
                _state["transcribing"] = False
                _set_runtime_state("idle")

    def _on_sigusr1(signum, frame):
        del signum, frame
        threading.Thread(target=_start_recording, daemon=True).start()

    def _on_sigusr2(signum, frame):
        del signum, frame
        threading.Thread(target=_stop_and_transcribe, daemon=True).start()

    signal.signal(signal.SIGUSR1, _on_sigusr1)
    signal.signal(signal.SIGUSR2, _on_sigusr2)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        with _lock:
            _state["recording"] = False
            _state["transcribing"] = False
            stream = _state["stream"]
            _state["stream"] = None
        if stream is not None:
            stream.stop()
            stream.close()
        _set_runtime_state("idle")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
