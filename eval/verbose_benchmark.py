#!/usr/bin/env python3
from __future__ import annotations

"""Very verbose, real-time benchmark runner for local Whisper model bakeoffs.

This script is intentionally chatty. It prints:
- model load timing
- sample start/end markers
- each emitted segment as decoding progresses
- per-sample WER/RTF
- running per-model aggregates
- final leaderboard and optional JSON output
"""

import argparse
import json
import re
import statistics
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from kdictate.audio_common import load_whisper_model
from kdictate.runtime_profile import (
    recommended_cpu_threads,
    recommended_shortform_cpu_threads,
    resolve_runtime,
    set_thread_env,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = PROJECT_ROOT / "eval/audio/manifest.json"
DEFAULT_RESULTS_ROOT = PROJECT_ROOT / "eval/results/verbose_benchmarks"


@dataclass(frozen=True)
class RunConfig:
    name: str
    model_dir: str
    cpu_threads: int
    beam_size: int = 1
    language: str = "en"
    compute_type: str = "int8"
    without_timestamps: bool = True
    vad_filter: bool = False
    condition_on_previous_text: bool = False
    best_of: int = 1
    temperature: float = 0.0
    no_speech_threshold: float = 0.6


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a very verbose real-time Whisper benchmark bakeoff.")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="Path to the eval manifest JSON.")
    parser.add_argument("--samples", type=int, default=20, help="Number of manifest entries to evaluate.")
    parser.add_argument(
        "--results-root",
        default=str(DEFAULT_RESULTS_ROOT),
        help="Directory where timestamped benchmark results are written.",
    )
    parser.add_argument("--tag", default=None, help="Optional suffix for the timestamped output directory.")
    parser.add_argument(
        "--preset",
        default="accuracy-bakeoff",
        choices=("accuracy-bakeoff",),
        help="Named model set to benchmark.",
    )
    parser.add_argument(
        "--skip-missing-models",
        action="store_true",
        help="Skip missing model directories instead of exiting.",
    )
    return parser.parse_args()


def accuracy_bakeoff_configs() -> list[RunConfig]:
    shortform_threads = recommended_shortform_cpu_threads()
    throughput_threads = recommended_cpu_threads()
    return [
        RunConfig(
            name=f"whisper_large_v3_t{shortform_threads}",
            model_dir="models/whisper-large-v3-ct2",
            cpu_threads=shortform_threads,
        ),
        RunConfig(
            name=f"whisper_large_v3_turbo_t{throughput_threads}",
            model_dir="models/whisper-large-v3-turbo-ct2",
            cpu_threads=throughput_threads,
        ),
        RunConfig(
            name=f"distil_large_v3_5_t{shortform_threads}",
            model_dir="models/distil-large-v3.5-ct2",
            cpu_threads=shortform_threads,
        ),
    ]


def configs_for_preset(name: str) -> list[RunConfig]:
    if name == "accuracy-bakeoff":
        return accuracy_bakeoff_configs()
    raise KeyError(name)


def load_manifest(path: Path, limit: int) -> list[dict]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    for item in manifest:
        audio_path = Path(item["path"])
        if not audio_path.is_absolute():
            item["path"] = str((PROJECT_ROOT / audio_path).resolve())
    return manifest[:limit]


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"(?<=\w)-(?=\w)", "", text)
    text = text.replace("'", "")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def word_error_rate(reference: str, hypothesis: str) -> float:
    ref_tokens = normalize_text(reference).split()
    hyp_tokens = normalize_text(hypothesis).split()

    if not ref_tokens:
        return 0.0 if not hyp_tokens else 1.0

    previous = list(range(len(hyp_tokens) + 1))
    for i, ref_token in enumerate(ref_tokens, start=1):
        current = [i]
        for j, hyp_token in enumerate(hyp_tokens, start=1):
            substitution_cost = 0 if ref_token == hyp_token else 1
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + substitution_cost,
                )
            )
        previous = current
    return previous[-1] / len(ref_tokens)


def print_rule(title: str) -> None:
    print()
    print("=" * 100)
    print(title)
    print("=" * 100)


def run_config(config: RunConfig, manifest: list[dict]) -> dict:
    model_path = PROJECT_ROOT / config.model_dir
    runtime = resolve_runtime("cpu", config.compute_type, config.cpu_threads)
    set_thread_env(runtime["cpu_threads"])

    print_rule(f"MODEL {config.name}")
    print(f"model_dir={model_path}")
    print(
        f"device={runtime['device']} compute_type={runtime['compute_type']} "
        f"cpu_threads={runtime['cpu_threads']} beam_size={config.beam_size} "
        f"vad_filter={config.vad_filter} without_timestamps={config.without_timestamps}"
    )

    load_start = time.perf_counter()
    model = load_whisper_model(
        model_path,
        device=runtime["device"],
        compute_type=runtime["compute_type"],
        cpu_threads=runtime["cpu_threads"],
    )
    model_load_seconds = time.perf_counter() - load_start
    print(f"model_load_seconds={model_load_seconds:.3f}")

    sample_results: list[dict] = []
    total_audio_seconds = 0.0
    total_decode_seconds = 0.0
    total_wer = 0.0

    for sample_index, item in enumerate(manifest, start=1):
        print()
        print(f"[{sample_index}/{len(manifest)}] sample_id={item['id']} path={item['path']}")
        print(f"reference={item['reference']}")
        print("-" * 100)

        sample_start = time.perf_counter()
        segments, info = model.transcribe(
            item["path"],
            language=config.language,
            task="transcribe",
            beam_size=config.beam_size,
            best_of=config.best_of,
            temperature=config.temperature,
            condition_on_previous_text=config.condition_on_previous_text,
            no_speech_threshold=config.no_speech_threshold,
            without_timestamps=config.without_timestamps,
            vad_filter=config.vad_filter,
        )

        text_parts: list[str] = []
        audio_seconds = 0.0
        segment_count = 0
        for segment_count, segment in enumerate(segments, start=1):
            clean_text = segment.text.strip()
            if clean_text:
                text_parts.append(clean_text)
            audio_seconds = max(audio_seconds, segment.end)
            elapsed_so_far = time.perf_counter() - sample_start
            print(
                f"segment={segment_count:02d} "
                f"t={segment.start:6.2f}->{segment.end:6.2f} "
                f"elapsed={elapsed_so_far:6.2f}s "
                f"text={clean_text}"
            )

        decode_seconds = time.perf_counter() - sample_start
        hypothesis = " ".join(text_parts).strip()
        wer = word_error_rate(item["reference"], hypothesis)
        rtf = (decode_seconds / audio_seconds) if audio_seconds > 0 else None

        total_audio_seconds += audio_seconds
        total_decode_seconds += decode_seconds
        total_wer += wer

        sample_payload = {
            "id": item["id"],
            "path": item["path"],
            "reference": item["reference"],
            "hypothesis": hypothesis,
            "detected_language": info.language,
            "language_confidence": round(info.language_probability, 4),
            "audio_seconds": round(audio_seconds, 3),
            "decode_seconds": round(decode_seconds, 3),
            "wer": round(wer, 5),
            "real_time_factor": round(rtf, 5) if rtf is not None else None,
            "segment_count": segment_count,
        }
        sample_results.append(sample_payload)

        avg_wer = total_wer / len(sample_results)
        overall_rtf = (total_decode_seconds / total_audio_seconds) if total_audio_seconds > 0 else None
        print("-" * 100)
        print(f"hypothesis={hypothesis}")
        print(
            f"sample_decode_seconds={decode_seconds:.3f} "
            f"sample_audio_seconds={audio_seconds:.3f} "
            f"sample_wer={wer:.4f} "
            f"sample_rtf={(rtf if rtf is not None else float('nan')):.4f}"
        )
        print(
            f"running_avg_wer={avg_wer:.4f} "
            f"running_overall_rtf={(overall_rtf if overall_rtf is not None else float('nan')):.4f}"
        )

    avg_wer = (total_wer / len(sample_results)) if sample_results else None
    overall_rtf = (total_decode_seconds / total_audio_seconds) if total_audio_seconds > 0 else None
    mean_decode_seconds = statistics.fmean(result["decode_seconds"] for result in sample_results) if sample_results else None

    print()
    print(
        f"MODEL_SUMMARY name={config.name} avg_wer={avg_wer:.4f} "
        f"overall_rtf={(overall_rtf if overall_rtf is not None else float('nan')):.4f} "
        f"mean_decode_seconds={(mean_decode_seconds if mean_decode_seconds is not None else float('nan')):.3f}"
    )

    return {
        "config": asdict(config),
        "runtime": runtime,
        "model_load_seconds": round(model_load_seconds, 3),
        "samples": len(sample_results),
        "avg_wer": round(avg_wer, 5) if avg_wer is not None else None,
        "overall_rtf": round(overall_rtf, 5) if overall_rtf is not None else None,
        "mean_decode_seconds": round(mean_decode_seconds, 3) if mean_decode_seconds is not None else None,
        "total_audio_seconds": round(total_audio_seconds, 3),
        "total_decode_seconds": round(total_decode_seconds, 3),
        "results": sample_results,
    }


def leaderboard_rows(run_payloads: list[dict]) -> list[dict]:
    ordered = sorted(run_payloads, key=lambda payload: (payload["avg_wer"], payload["overall_rtf"]))
    rows = []
    for rank, payload in enumerate(ordered, start=1):
        rows.append(
            {
                "rank": rank,
                "name": payload["config"]["name"],
                "model_dir": payload["config"]["model_dir"],
                "cpu_threads": payload["config"]["cpu_threads"],
                "avg_wer": payload["avg_wer"],
                "overall_rtf": payload["overall_rtf"],
                "mean_decode_seconds": payload["mean_decode_seconds"],
                "model_load_seconds": payload["model_load_seconds"],
            }
        )
    return rows


def main() -> int:
    args = parse_args()
    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        print(f"Manifest not found: {manifest_path}", file=sys.stderr)
        return 1

    manifest = load_manifest(manifest_path, args.samples)
    if not manifest:
        print("Manifest is empty.", file=sys.stderr)
        return 1

    try:
        configs = configs_for_preset(args.preset)
    except KeyError:
        print(f"Unknown preset: {args.preset}", file=sys.stderr)
        return 1

    selected_configs = []
    for config in configs:
        model_path = PROJECT_ROOT / config.model_dir
        if model_path.exists():
            selected_configs.append(config)
            continue
        if args.skip_missing_models:
            print(f"Skipping missing model_dir={model_path}", file=sys.stderr)
            continue
        print(f"Model not found: {model_path}", file=sys.stderr)
        return 1

    if not selected_configs:
        print("No runnable model configs remain.", file=sys.stderr)
        return 1

    results_root = Path(args.results_root)
    results_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir_name = f"{timestamp}_{args.tag}" if args.tag else timestamp
    output_dir = results_root / output_dir_name
    output_dir.mkdir(parents=True, exist_ok=False)

    print_rule("VERBOSE BENCHMARK")
    print(f"manifest={manifest_path}")
    print(f"samples={len(manifest)}")
    print(f"preset={args.preset}")
    print(f"output_dir={output_dir}")
    print("configs=" + ", ".join(config.name for config in selected_configs))

    run_payloads = []
    for config in selected_configs:
        payload = run_config(config, manifest)
        run_payloads.append(payload)
        out_path = output_dir / f"{config.name}.json"
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    rows = leaderboard_rows(run_payloads)
    summary = {
        "manifest": str(manifest_path),
        "samples": len(manifest),
        "preset": args.preset,
        "output_dir": str(output_dir),
        "leaderboard": rows,
        "runs": run_payloads,
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print_rule("FINAL LEADERBOARD")
    for row in rows:
        print(
            f"rank={row['rank']} "
            f"name={row['name']} "
            f"avg_wer={row['avg_wer']:.4f} "
            f"overall_rtf={row['overall_rtf']:.4f} "
            f"mean_decode_seconds={row['mean_decode_seconds']:.3f} "
            f"model_load_seconds={row['model_load_seconds']:.3f}"
        )
    print(f"results_written_to={output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
