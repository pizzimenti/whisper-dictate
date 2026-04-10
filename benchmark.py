from __future__ import annotations

"""Simple repeatable benchmark harness for Whisper decode throughput."""

import argparse
import statistics
import time
from pathlib import Path

from kdictate.offline_common import (
    add_shared_runtime_args,
    load_offline_model,
    resolve_input_paths,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark Whisper transcription speed.")
    parser.add_argument("audio", help="Path to input audio file.")
    add_shared_runtime_args(parser)
    parser.add_argument("--runs", type=int, default=3, help="Measured runs.")
    parser.add_argument("--warmup", type=int, default=1, help="Warmup runs.")
    parser.add_argument("--beam-size", type=int, default=1, help="Beam size.")
    parser.add_argument(
        "--vad-filter",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable built-in VAD filtering.",
    )
    return parser.parse_args()


def run_once(model, audio_path: Path, language: str | None, beam_size: int, vad_filter: bool) -> tuple[float, float]:
    # We consume all segments to force full decode before measuring elapsed time.
    start = time.perf_counter()
    segments, _ = model.transcribe(
        str(audio_path),
        language=language,
        beam_size=beam_size,
        best_of=1,
        temperature=0.0,
        condition_on_previous_text=False,
        vad_filter=vad_filter,
        without_timestamps=True,
        word_timestamps=False,
    )
    audio_duration = 0.0
    for segment in segments:
        if segment.end > audio_duration:
            audio_duration = segment.end
    elapsed = time.perf_counter() - start
    return elapsed, audio_duration


def main() -> int:
    args = parse_args()
    try:
        audio_path, model_dir = resolve_input_paths(args.audio, args.model_dir)
    except FileNotFoundError as exc:
        print(str(exc))
        print("Prepare the default model first: python prepare_model.py")
        return 1

    model, runtime, load_seconds = load_offline_model(
        model_dir,
        device=args.device,
        compute_type=args.compute_type,
        cpu_threads=args.cpu_threads,
    )

    # Warmup runs stabilize caches and one-time kernel setup costs.
    for _ in range(max(0, args.warmup)):
        run_once(model, audio_path, args.language, args.beam_size, args.vad_filter)

    run_times = []
    rtfs = []
    for i in range(max(1, args.runs)):
        elapsed, audio_duration = run_once(model, audio_path, args.language, args.beam_size, args.vad_filter)
        rtf = (elapsed / audio_duration) if audio_duration > 0 else float("inf")
        run_times.append(elapsed)
        rtfs.append(rtf)
        speed = (1.0 / rtf) if rtf > 0 and rtf != float("inf") else 0.0
        print(
            f"run={i + 1} transcribe_seconds={elapsed:.2f} audio_seconds={audio_duration:.2f} "
            f"rtf={rtf:.3f} x_realtime={speed:.2f}"
        )

    # Averages are enough for quick tuning; use more runs for tighter confidence.
    avg_time = statistics.mean(run_times)
    avg_rtf = statistics.mean(rtfs)
    speed = (1.0 / avg_rtf) if avg_rtf > 0 and avg_rtf != float("inf") else 0.0

    print("\nSUMMARY")
    print("-------")
    print(f"device={runtime['device']} compute_type={runtime['compute_type']} cpu_threads={runtime['cpu_threads']}")
    print(f"model_load_seconds={load_seconds:.2f}")
    print(f"runs={len(run_times)} warmup={max(0, args.warmup)}")
    print(f"avg_transcribe_seconds={avg_time:.2f}")
    print(f"avg_real_time_factor={avg_rtf:.3f}")
    print(f"avg_x_realtime={speed:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
