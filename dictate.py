from __future__ import annotations

"""Whisper-KDE system dictation daemon.

Send SIGUSR1 to toggle recording on/off. On stop, transcribes the captured
audio and types the result into the focused window via wtype.

Intended to run as a long-lived process (e.g. systemd user service) so the
model stays loaded in memory between recordings.
"""

import argparse
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from runtime_profile import resolve_runtime, set_thread_env


def _notify(msg: str) -> None:
    subprocess.Popen(
        ["notify-send", "-a", "whisper-dictate", "-t", "3000", msg],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _type_text(text: str) -> None:
    subprocess.run(["wtype", "--", text], check=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Whisper-KDE dictation daemon. Toggle recording with SIGUSR1."
    )
    parser.add_argument(
        "--model-dir",
        default=str(Path(__file__).parent / "models/distil-large-v3-ct2-int8"),
        help="Path to CTranslate2 model directory.",
    )
    parser.add_argument("--language", default="en", help="Language code (e.g. en, es, fr).")
    parser.add_argument("--sample-rate", type=int, default=16000, help="Microphone sample rate.")
    parser.add_argument("--beam-size", type=int, default=5, help="Whisper beam size.")
    parser.add_argument("--cpu-threads", type=int, default=None, help="Override CPU thread count.")
    parser.add_argument(
        "--compute-type",
        default=None,
        choices=("float32", "float16", "int8", "int8_float16"),
        help="Compute type. If omitted, auto-selects.",
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
    )
    print(
        f"Model ready. device={runtime['device']} compute_type={runtime['compute_type']} "
        f"cpu_threads={runtime['cpu_threads']}",
        flush=True,
    )
    _notify("Whisper-KDE ready")

    _lock = threading.Lock()
    _state: dict = {"recording": False, "chunks": [], "stream": None}

    def _start_recording() -> None:
        import numpy as np
        import sounddevice as sd

        with _lock:
            if _state["recording"]:
                return
            _state["recording"] = True
            _state["chunks"] = []

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
            chunks = list(_state["chunks"])
            stream = _state["stream"]
            _state["stream"] = None

        if stream is not None:
            stream.stop()
            stream.close()

        if not chunks:
            _notify("Nothing recorded.")
            return

        print("Transcribing...", flush=True)
        _notify("Transcribing...")

        audio = np.concatenate(chunks).astype(np.float32) / 32768.0
        audio = audio.clip(-1.0, 1.0)
        segments, _ = model.transcribe(audio, language=args.language, beam_size=args.beam_size)
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

    def _on_sigusr1(signum, frame):
        with _lock:
            currently_recording = _state["recording"]
        if currently_recording:
            threading.Thread(target=_stop_and_transcribe, daemon=True).start()
        else:
            threading.Thread(target=_start_recording, daemon=True).start()

    signal.signal(signal.SIGUSR1, _on_sigusr1)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
