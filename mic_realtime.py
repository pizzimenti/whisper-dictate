from __future__ import annotations

"""Live microphone transcription loop for terminal-only output.

Behavior goals:
- Read from default system mic.
- Print recognized text as utterances complete.
- Keep latency low with chunked capture + simple energy-based segmentation.
- Exit cleanly as soon as the user presses Enter.
"""

import argparse
import queue
import threading
import time
from pathlib import Path

from runtime_profile import resolve_runtime, set_thread_env


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Real-time Whisper V3 Turbo transcription from default microphone. Press Enter to stop."
    )
    parser.add_argument(
        "--model-dir",
        default="models/whisper-large-v3-turbo-ct2-int8",
        help="Path to converted CTranslate2 model directory.",
    )
    parser.add_argument(
        "--device",
        default="cpu",
        choices=("auto", "cpu"),
        help="Inference device (CPU only in this project).",
    )
    parser.add_argument(
        "--compute-type",
        default=None,
        choices=("float32", "float16", "int8", "int8_float16"),
        help="faster-whisper compute type. If omitted, auto-selects.",
    )
    parser.add_argument("--cpu-threads", type=int, default=None, help="Override CPU thread count.")
    parser.add_argument("--language", default=None, help="Language code, for example: en, es, fr.")
    parser.add_argument("--sample-rate", type=int, default=16000, help="Microphone capture sample rate.")
    parser.add_argument("--block-ms", type=int, default=30, help="Capture block duration in milliseconds.")
    parser.add_argument(
        "--energy-threshold",
        type=float,
        default=400.0,
        help="RMS threshold for speech detection. Increase to ignore noise.",
    )
    parser.add_argument(
        "--silence-ms",
        type=int,
        default=500,
        help="Silence duration that finalizes and transcribes the current utterance.",
    )
    parser.add_argument(
        "--min-speech-ms",
        type=int,
        default=250,
        help="Minimum speech duration required to transcribe an utterance.",
    )
    parser.add_argument(
        "--max-utterance-s",
        type=float,
        default=12.0,
        help="Force transcription when an utterance reaches this length.",
    )
    parser.add_argument("--beam-size", type=int, default=1, help="Beam size (1 is fastest).")
    return parser.parse_args()


def _wait_for_enter(stop_event: threading.Event) -> None:
    """Block on stdin and signal shutdown once Enter is pressed."""
    try:
        input()
    except EOFError:
        pass
    stop_event.set()


def _transcribe_utterance(model, utterance_pcm: list["np.ndarray"], language: str | None, beam_size: int) -> None:
    """Run Whisper on one buffered utterance and print final text."""
    import numpy as np

    if not utterance_pcm:
        return

    # Join all collected int16 chunks into one contiguous utterance.
    audio_pcm = np.concatenate(utterance_pcm, axis=0)
    if audio_pcm.size == 0:
        return

    # `faster-whisper` expects float32 PCM in [-1.0, 1.0].
    audio_f32 = (audio_pcm.astype(np.float32) / 32768.0).clip(-1.0, 1.0)
    segments, _ = model.transcribe(
        audio_f32,
        language=language,
        task="transcribe",
        beam_size=beam_size,
        best_of=1,
        temperature=0.0,
        condition_on_previous_text=False,
        vad_filter=False,
        word_timestamps=False,
    )
    # Collapse all segment text from this utterance to a single terminal line.
    text = " ".join(s.text.strip() for s in segments if s.text and s.text.strip()).strip()
    if text:
        now = time.strftime("%H:%M:%S")
        print(f"[{now}] {text}", flush=True)


def main() -> int:
    args = parse_args()
    runtime = resolve_runtime(args.device, args.compute_type, args.cpu_threads)
    set_thread_env(runtime["cpu_threads"])

    model_dir = Path(args.model_dir)
    if not model_dir.exists():
        print(f"Model directory not found: {model_dir}")
        print("Create it once with: python prepare_model.py")
        return 1

    try:
        import numpy as np
        import sounddevice as sd
    except ImportError as exc:
        print(f"Missing dependency: {exc.name}")
        print("Install dependencies first: pip install -r requirements.txt")
        return 1

    from faster_whisper import WhisperModel

    # Load model once; reuse it for all utterances.
    model = WhisperModel(
        str(model_dir),
        device=runtime["device"],
        compute_type=runtime["compute_type"],
        cpu_threads=runtime["cpu_threads"],
        num_workers=1,
    )

    # Convert timing knobs (ms/s) into block counts used by the stream loop.
    block_size = max(1, int(args.sample_rate * (args.block_ms / 1000.0)))
    silence_blocks = max(1, int(args.silence_ms / args.block_ms))
    min_speech_blocks = max(1, int(args.min_speech_ms / args.block_ms))
    max_utterance_blocks = max(1, int((args.max_utterance_s * 1000.0) / args.block_ms))

    # Audio callback pushes chunks quickly; main thread drains and processes.
    # This decouples real-time capture from potentially slower transcription.
    q: queue.Queue[np.ndarray] = queue.Queue(maxsize=512)
    stop_event = threading.Event()
    enter_thread = threading.Thread(target=_wait_for_enter, args=(stop_event,), daemon=True)
    enter_thread.start()

    print(
        "Live transcription started. Press Enter to stop.\n"
        f"device={runtime['device']} compute_type={runtime['compute_type']} cpu_threads={runtime['cpu_threads']}",
        flush=True,
    )

    utterance_pcm: list[np.ndarray] = []
    in_speech = False
    speech_block_count = 0
    trailing_silence_count = 0

    def audio_callback(indata, frames, callback_time, status) -> None:
        """PortAudio callback: copy mono block and enqueue without blocking."""
        del frames, callback_time
        if status:
            # Stream warnings are ignored to keep callback non-blocking.
            return
        chunk = indata[:, 0].copy()
        try:
            q.put_nowait(chunk)
        except queue.Full:
            # Drop audio if producer outruns consumer to keep stream live.
            pass

    stream = sd.InputStream(
        samplerate=args.sample_rate,
        channels=1,
        dtype="int16",
        blocksize=block_size,
        callback=audio_callback,
    )

    with stream:
        while True:
            if stop_event.is_set() and q.empty():
                break
            try:
                chunk = q.get(timeout=0.05)
            except queue.Empty:
                continue

            # Lightweight VAD: RMS above threshold marks a voiced block.
            rms = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
            voiced = rms >= args.energy_threshold

            # State machine:
            # - Enter speech on first voiced block.
            # - Keep buffering voiced blocks.
            # - While in speech, keep buffering trailing silence so the
            #   utterance preserves natural end-of-phrase context.
            if voiced:
                if not in_speech:
                    in_speech = True
                    speech_block_count = 0
                    trailing_silence_count = 0
                    utterance_pcm = []
                utterance_pcm.append(chunk)
                speech_block_count += 1
                trailing_silence_count = 0
            elif in_speech:
                utterance_pcm.append(chunk)
                trailing_silence_count += 1

            # Hard cap prevents very long utterances from delaying output.
            if in_speech and speech_block_count >= max_utterance_blocks:
                if speech_block_count >= min_speech_blocks:
                    _transcribe_utterance(model, utterance_pcm, args.language, args.beam_size)
                in_speech = False
                speech_block_count = 0
                trailing_silence_count = 0
                utterance_pcm = []
                continue

            # Standard commit path: enough trailing silence marks utterance end.
            if in_speech and trailing_silence_count >= silence_blocks:
                if speech_block_count >= min_speech_blocks:
                    _transcribe_utterance(model, utterance_pcm, args.language, args.beam_size)
                in_speech = False
                speech_block_count = 0
                trailing_silence_count = 0
                utterance_pcm = []

    # Flush any buffered speech when user stops capture mid-utterance.
    if in_speech and speech_block_count >= min_speech_blocks and utterance_pcm:
        _transcribe_utterance(model, utterance_pcm, args.language, args.beam_size)

    print("Stopped.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
