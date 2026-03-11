#!/usr/bin/env python3
from __future__ import annotations

"""Run an exhaustive distil-medium-en parameter sweep on the local eval set.

This sweep intentionally focuses on the knobs that can materially affect speed
or accuracy on the current CPU-only runtime:
- cpu_threads: short-form vs all logical cores
- compute_type: int8 vs float32
- beam_size: 1 vs 5
- without_timestamps: True vs False
- vad_filter: False vs True
- condition_on_previous_text: False vs True

That yields 64 configs over the fixed 20-sample local LibriSpeech subset.
"""

import argparse
import csv
import itertools
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

from runtime_profile import recommended_cpu_threads, recommended_shortform_cpu_threads, resolve_runtime, set_thread_env

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = PROJECT_ROOT / "eval/audio/manifest.json"
DEFAULT_RESULTS_ROOT = PROJECT_ROOT / "eval/results/exhaustive"
DEFAULT_MODEL_DIR = PROJECT_ROOT / "models/distil-medium-en-ct2-int8"


@dataclass(frozen=True)
class RunConfig:
    name: str
    model_dir: str
    cpu_threads: int
    compute_type: str
    beam_size: int
    without_timestamps: bool
    vad_filter: bool
    condition_on_previous_text: bool
    best_of: int = 1
    temperature: float = 0.0
    no_speech_threshold: float = 0.6
    language: str = "en"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run an exhaustive distil-medium-en parameter sweep.")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="Path to the eval manifest JSON.")
    parser.add_argument("--samples", type=int, default=20, help="Number of manifest entries to evaluate.")
    parser.add_argument(
        "--results-root",
        default=str(DEFAULT_RESULTS_ROOT),
        help="Directory where timestamped sweep results are written.",
    )
    parser.add_argument("--tag", default=None, help="Optional suffix for the timestamped output directory.")
    return parser.parse_args()


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


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = pct * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def format_bool(value: bool) -> str:
    return "yes" if value else "no"


def default_configs() -> list[RunConfig]:
    short_threads = recommended_shortform_cpu_threads()
    full_threads = recommended_cpu_threads()
    configs = []
    for cpu_threads, compute_type, beam_size, without_timestamps, vad_filter, condition_on_previous_text in itertools.product(
        (short_threads, full_threads),
        ("int8", "float32"),
        (1, 5),
        (True, False),
        (False, True),
        (False, True),
    ):
        name = (
            f"dm_"
            f"{compute_type}_"
            f"b{beam_size}_"
            f"{'nots' if without_timestamps else 'ts'}_"
            f"{'vad' if vad_filter else 'novad'}_"
            f"{'prev' if condition_on_previous_text else 'noprev'}_"
            f"t{cpu_threads}"
        )
        configs.append(
            RunConfig(
                name=name,
                model_dir=str(DEFAULT_MODEL_DIR.relative_to(PROJECT_ROOT)),
                cpu_threads=cpu_threads,
                compute_type=compute_type,
                beam_size=beam_size,
                without_timestamps=without_timestamps,
                vad_filter=vad_filter,
                condition_on_previous_text=condition_on_previous_text,
            )
        )
    return configs


def load_manifest(path: Path, limit: int) -> list[dict]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    return manifest[:limit]


def run_config(config: RunConfig, manifest: list[dict]) -> dict:
    runtime = resolve_runtime("cpu", config.compute_type, config.cpu_threads)
    set_thread_env(runtime["cpu_threads"])

    model_path = PROJECT_ROOT / config.model_dir
    if not model_path.exists():
        raise FileNotFoundError(f"Model not found: {model_path}")

    from faster_whisper import WhisperModel

    load_start = time.perf_counter()
    model = WhisperModel(
        str(model_path),
        device=runtime["device"],
        compute_type=runtime["compute_type"],
        cpu_threads=runtime["cpu_threads"],
        num_workers=1,
    )
    model_load_s = time.perf_counter() - load_start

    sample_results = []
    decode_times = []
    short_decode_times = []
    short_rtfs = []
    total_audio_s = 0.0
    total_decode_s = 0.0
    total_wer = 0.0

    for item in manifest:
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

        text_parts = []
        audio_duration_s = 0.0
        for segment in segments:
            clean_text = segment.text.strip()
            if clean_text:
                text_parts.append(clean_text)
            if segment.end > audio_duration_s:
                audio_duration_s = segment.end

        decode_s = time.perf_counter() - sample_start
        hypothesis = " ".join(text_parts).strip()
        wer = word_error_rate(item["reference"], hypothesis)
        rtf = (decode_s / audio_duration_s) if audio_duration_s > 0 else None

        total_audio_s += audio_duration_s
        total_decode_s += decode_s
        total_wer += wer
        decode_times.append(decode_s)

        if audio_duration_s <= 4.0:
            short_decode_times.append(decode_s)
            if rtf is not None:
                short_rtfs.append(rtf)

        sample_results.append(
            {
                "id": item["id"],
                "path": item["path"],
                "reference": item["reference"],
                "reference_normalized": normalize_text(item["reference"]),
                "hypothesis": hypothesis,
                "hypothesis_normalized": normalize_text(hypothesis),
                "detected_language": info.language,
                "language_confidence": round(info.language_probability, 4),
                "audio_seconds": round(audio_duration_s, 3),
                "decode_seconds": round(decode_s, 3),
                "real_time_factor": round(rtf, 5) if rtf is not None else None,
                "wer": round(wer, 5),
            }
        )

    samples = len(sample_results)
    overall_rtf = (total_decode_s / total_audio_s) if total_audio_s > 0 else None
    avg_wer = (total_wer / samples) if samples else None
    mean_decode_s = statistics.fmean(decode_times) if decode_times else None
    median_decode_s = statistics.median(decode_times) if decode_times else None
    p90_decode_s = percentile(decode_times, 0.9) if decode_times else None
    short_mean_decode_s = statistics.fmean(short_decode_times) if short_decode_times else None
    short_mean_rtf = statistics.fmean(short_rtfs) if short_rtfs else None
    first_short_result_s = (model_load_s + short_mean_decode_s) if short_mean_decode_s is not None else None

    return {
        "config": asdict(config),
        "runtime": runtime,
        "model_load_seconds": round(model_load_s, 3),
        "samples": samples,
        "avg_wer": round(avg_wer, 5) if avg_wer is not None else None,
        "overall_rtf": round(overall_rtf, 5) if overall_rtf is not None else None,
        "speed_x_realtime": round((1.0 / overall_rtf), 3) if overall_rtf not in (None, 0) else None,
        "mean_decode_seconds": round(mean_decode_s, 3) if mean_decode_s is not None else None,
        "median_decode_seconds": round(median_decode_s, 3) if median_decode_s is not None else None,
        "p90_decode_seconds": round(p90_decode_s, 3) if p90_decode_s is not None else None,
        "short_clip_count": len(short_decode_times),
        "short_clip_mean_decode_seconds": round(short_mean_decode_s, 3) if short_mean_decode_s is not None else None,
        "short_clip_mean_rtf": round(short_mean_rtf, 5) if short_mean_rtf is not None else None,
        "first_short_result_seconds": round(first_short_result_s, 3) if first_short_result_s is not None else None,
        "total_audio_seconds": round(total_audio_s, 3),
        "total_decode_seconds": round(total_decode_s, 3),
        "results": sample_results,
    }


def pareto_frontier(rows: list[dict]) -> list[dict]:
    frontier = []
    for row in sorted(rows, key=lambda x: (x["avg_wer"], x["short_clip_mean_decode_seconds"])):
        dominated = False
        for other in rows:
            if other is row:
                continue
            if (
                other["avg_wer"] <= row["avg_wer"]
                and other["short_clip_mean_decode_seconds"] <= row["short_clip_mean_decode_seconds"]
                and (
                    other["avg_wer"] < row["avg_wer"]
                    or other["short_clip_mean_decode_seconds"] < row["short_clip_mean_decode_seconds"]
                )
            ):
                dominated = True
                break
        if not dominated:
            frontier.append(row)
    return frontier


def leaderboard_rows(run_payloads: list[dict]) -> list[dict]:
    rows = []
    for payload in run_payloads:
        config = payload["config"]
        rows.append(
            {
                "name": config["name"],
                "compute_type": config["compute_type"],
                "beam_size": config["beam_size"],
                "cpu_threads": config["cpu_threads"],
                "without_timestamps": format_bool(config["without_timestamps"]),
                "vad_filter": format_bool(config["vad_filter"]),
                "condition_on_previous_text": format_bool(config["condition_on_previous_text"]),
                "avg_wer": payload["avg_wer"],
                "overall_rtf": payload["overall_rtf"],
                "speed_x_realtime": payload["speed_x_realtime"],
                "mean_decode_seconds": payload["mean_decode_seconds"],
                "median_decode_seconds": payload["median_decode_seconds"],
                "p90_decode_seconds": payload["p90_decode_seconds"],
                "short_clip_mean_decode_seconds": payload["short_clip_mean_decode_seconds"],
                "short_clip_mean_rtf": payload["short_clip_mean_rtf"],
                "model_load_seconds": payload["model_load_seconds"],
                "first_short_result_seconds": payload["first_short_result_seconds"],
            }
        )

    return sorted(
        rows,
        key=lambda row: (
            row["avg_wer"],
            row["short_clip_mean_decode_seconds"],
            row["model_load_seconds"],
        ),
    )


def write_csv(path: Path, rows: list[dict]) -> None:
    fieldnames = [
        "name",
        "compute_type",
        "beam_size",
        "cpu_threads",
        "without_timestamps",
        "vad_filter",
        "condition_on_previous_text",
        "avg_wer",
        "overall_rtf",
        "speed_x_realtime",
        "mean_decode_seconds",
        "median_decode_seconds",
        "p90_decode_seconds",
        "short_clip_mean_decode_seconds",
        "short_clip_mean_rtf",
        "model_load_seconds",
        "first_short_result_seconds",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(path: Path, rows: list[dict], frontier: list[dict]) -> None:
    lines = [
        "| name | compute | beam | threads | no_ts | vad | prev | avg_wer | overall_rtf | short_s | load_s | first_short_s |",
        "| --- | --- | ---: | ---: | :---: | :---: | :---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            "| {name} | {compute_type} | {beam_size} | {cpu_threads} | {without_timestamps} | "
            "{vad_filter} | {condition_on_previous_text} | {avg_wer:.4f} | {overall_rtf:.4f} | "
            "{short_clip_mean_decode_seconds:.3f} | {model_load_seconds:.3f} | {first_short_result_seconds:.3f} |".format(
                **row
            )
        )

    lines.append("")
    lines.append("## Pareto Frontier")
    lines.append("")
    for row in frontier:
        lines.append(
            "- {name}: wer={avg_wer:.4f} short_s={short_clip_mean_decode_seconds:.3f} load_s={model_load_seconds:.3f}".format(
                **row
            )
        )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def print_frontier(frontier: list[dict]) -> None:
    print()
    print("Pareto frontier (accuracy vs short-clip latency):")
    for row in frontier:
        print(
            f"  {row['name']}: wer={row['avg_wer']:.4f} "
            f"short_s={row['short_clip_mean_decode_seconds']:.3f} "
            f"load_s={row['model_load_seconds']:.3f}"
        )


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

    results_root = Path(args.results_root)
    results_root.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir_name = f"{timestamp}_{args.tag}" if args.tag else timestamp
    output_dir = results_root / output_dir_name
    output_dir.mkdir(parents=True, exist_ok=False)

    run_payloads = []
    for index, config in enumerate(default_configs(), start=1):
        print(f"[{index:02d}/64] Running {config.name}...", flush=True)
        payload = run_config(config, manifest)
        run_payloads.append(payload)
        out_path = output_dir / f"{config.name}.json"
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(
            f"  wer={payload['avg_wer']:.4f} "
            f"rtf={payload['overall_rtf']:.3f} "
            f"short_s={payload['short_clip_mean_decode_seconds']:.3f} "
            f"load_s={payload['model_load_seconds']:.3f}",
            flush=True,
        )

    rows = leaderboard_rows(run_payloads)
    frontier = pareto_frontier(rows)
    summary_payload = {
        "manifest": str(manifest_path),
        "samples": len(manifest),
        "output_dir": str(output_dir),
        "frontier": frontier,
        "leaderboard": rows,
        "runs": run_payloads,
        "assumptions": {
            "model": "distil-medium-en-ct2-int8",
            "swept_axes": {
                "cpu_threads": sorted({row["cpu_threads"] for row in rows}),
                "compute_type": sorted({row["compute_type"] for row in rows}),
                "beam_size": sorted({row["beam_size"] for row in rows}),
                "without_timestamps": sorted({row["without_timestamps"] for row in rows}),
                "vad_filter": sorted({row["vad_filter"] for row in rows}),
                "condition_on_previous_text": sorted({row["condition_on_previous_text"] for row in rows}),
            },
        },
    }

    (output_dir / "summary.json").write_text(json.dumps(summary_payload, indent=2), encoding="utf-8")
    write_csv(output_dir / "leaderboard.csv", rows)
    write_markdown(output_dir / "leaderboard.md", rows, frontier)
    print_frontier(frontier)
    print(f"\nResults written to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
