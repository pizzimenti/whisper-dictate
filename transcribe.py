from __future__ import annotations

"""Offline file transcription entrypoint.

This script is separate from live mic mode and is mainly used for deterministic
benchmarking, debugging decode settings, and producing optional JSON output.
"""

import argparse
import json
import time
from pathlib import Path

from kdictate.offline_common import (
    add_shared_runtime_args,
    load_offline_model,
    resolve_input_paths,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Transcribe audio with Whisper (CTranslate2/faster-whisper).")
    parser.add_argument("audio", help="Path to input audio file.")
    add_shared_runtime_args(parser)
    parser.add_argument("--task", default="transcribe", choices=("transcribe", "translate"), help="Whisper task.")
    parser.add_argument("--beam-size", type=int, default=1, help="Beam size (1 is fastest).")
    parser.add_argument("--best-of", type=int, default=1, help="Candidates when temperature > 0.")
    parser.add_argument("--temperature", type=float, default=0.0, help="Sampling temperature.")
    parser.add_argument(
        "--condition-on-previous-text",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Condition on previous segment text (can improve coherence, usually slower).",
    )
    parser.add_argument(
        "--vad-filter",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable Whisper's built-in VAD filtering. On by default for offline transcription where there is no external VAD and it improves accuracy on files with silence.",
    )
    parser.add_argument(
        "--word-timestamps",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Emit per-word timestamps (slower).",
    )
    parser.add_argument("--initial-prompt", default=None, help="Optional initial prompt.")
    parser.add_argument("--output-json", default=None, help="Optional path to write JSON result.")
    return parser.parse_args()


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

    # Decode timing starts here so reported throughput reflects transcription only.
    run_start = time.perf_counter()
    segments, info = model.transcribe(
        str(audio_path),
        language=args.language,
        task=args.task,
        beam_size=args.beam_size,
        best_of=args.best_of,
        temperature=args.temperature,
        condition_on_previous_text=args.condition_on_previous_text,
        vad_filter=args.vad_filter,
        without_timestamps=not args.word_timestamps,
        word_timestamps=args.word_timestamps,
        initial_prompt=args.initial_prompt,
    )

    transcript_segments = []
    audio_duration = 0.0
    for index, segment in enumerate(segments):
        clean_text = segment.text.strip()
        transcript_segments.append(
            {
                "id": index,
                "start": round(segment.start, 3),
                "end": round(segment.end, 3),
                "text": clean_text,
            }
        )
        if segment.end > audio_duration:
            audio_duration = segment.end

    run_seconds = time.perf_counter() - run_start
    transcript_text = " ".join(s["text"] for s in transcript_segments if s["text"]).strip()
    # Real-time factor (RTF): <1.0 means faster than real-time.
    rtf = (run_seconds / audio_duration) if audio_duration > 0 else None

    print(f"device={runtime['device']} compute_type={runtime['compute_type']} cpu_threads={runtime['cpu_threads']}")
    print(f"detected_language={info.language} confidence={info.language_probability:.3f}")
    print(f"model_load_seconds={load_seconds:.2f}")
    print(f"transcribe_seconds={run_seconds:.2f}")
    if rtf is not None:
        print(f"audio_seconds={audio_duration:.2f}")
        print(f"real_time_factor={rtf:.3f}")
        print(f"x_realtime={(1.0 / rtf):.2f}")
    print("\nTRANSCRIPT\n----------")
    print(transcript_text)

    if args.output_json:
        out_path = Path(args.output_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Keep JSON schema stable for downstream scripting.
        payload = {
            "runtime": runtime,
            "timing": {
                "model_load_seconds": round(load_seconds, 3),
                "transcribe_seconds": round(run_seconds, 3),
                "audio_seconds": round(audio_duration, 3),
                "real_time_factor": round(rtf, 5) if rtf is not None else None,
            },
            "detected_language": info.language,
            "language_confidence": info.language_probability,
            "text": transcript_text,
            "segments": transcript_segments,
        }
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nSaved JSON to {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
