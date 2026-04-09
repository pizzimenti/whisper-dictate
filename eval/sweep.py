#!/usr/bin/env python3
from __future__ import annotations

"""Run a curated dictation-focused evaluation sweep over the local LibriSpeech set.

The sweep writes:
- one JSON file per config with full per-sample transcripts and timings
- a summary JSON with all configs
- a CSV + Markdown leaderboard for quick comparison
"""

import argparse
import csv
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

from whisper_dictate.audio_common import load_whisper_model
from whisper_dictate.runtime_profile import (
    recommended_cpu_threads,
    recommended_shortform_cpu_threads,
    resolve_runtime,
    set_thread_env,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = PROJECT_ROOT / "eval/audio/manifest.json"
DEFAULT_RESULTS_ROOT = PROJECT_ROOT / "eval/results/sweeps"


@dataclass(frozen=True)
class RunConfig:
    name: str
    model_dir: str
    beam_size: int
    cpu_threads: int
    without_timestamps: bool
    vad_filter: bool
    condition_on_previous_text: bool = False
    no_speech_threshold: float = 0.6
    best_of: int = 1
    temperature: float = 0.0
    language: str = "en"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a curated sweep of local whisper-dictate settings.")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="Path to the eval manifest JSON.")
    parser.add_argument("--samples", type=int, default=20, help="Number of manifest entries to evaluate.")
    parser.add_argument(
        "--results-root",
        default=str(DEFAULT_RESULTS_ROOT),
        help="Directory where timestamped sweep results are written.",
    )
    parser.add_argument("--tag", default=None, help="Optional suffix for the timestamped output directory.")
    parser.add_argument(
        "--preset",
        default="default",
        help="Named config preset to run. Use --list-presets to inspect available presets.",
    )
    parser.add_argument(
        "--list-presets",
        action="store_true",
        help="Print available presets and exit.",
    )
    parser.add_argument(
        "--config-name",
        action="append",
        default=[],
        help="Run only the named config(s). May be provided multiple times.",
    )
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


def default_configs() -> list[RunConfig]:
    full_threads = recommended_cpu_threads()
    half_threads = max(1, full_threads // 2)
    return [
        RunConfig(
            name=f"distil_medium_beam1_nots_t{full_threads}",
            model_dir="models/distil-medium-en-ct2-int8",
            beam_size=1,
            cpu_threads=full_threads,
            without_timestamps=True,
            vad_filter=False,
        ),
        RunConfig(
            name=f"distil_medium_beam1_nots_t{half_threads}",
            model_dir="models/distil-medium-en-ct2-int8",
            beam_size=1,
            cpu_threads=half_threads,
            without_timestamps=True,
            vad_filter=False,
        ),
        RunConfig(
            name=f"distil_medium_beam5_nots_t{full_threads}",
            model_dir="models/distil-medium-en-ct2-int8",
            beam_size=5,
            cpu_threads=full_threads,
            without_timestamps=True,
            vad_filter=False,
        ),
        RunConfig(
            name=f"distil_medium_beam1_nots_vad_t{full_threads}",
            model_dir="models/distil-medium-en-ct2-int8",
            beam_size=1,
            cpu_threads=full_threads,
            without_timestamps=True,
            vad_filter=True,
        ),
        RunConfig(
            name=f"distil_medium_beam5_nots_vad_t{half_threads}",
            model_dir="models/distil-medium-en-ct2-int8",
            beam_size=5,
            cpu_threads=half_threads,
            without_timestamps=True,
            vad_filter=True,
        ),
    ]


def accuracy_bakeoff_configs() -> list[RunConfig]:
    shortform_threads = recommended_shortform_cpu_threads()
    throughput_threads = recommended_cpu_threads()
    return [
        RunConfig(
            name=f"whisper_large_v3_beam1_nots_t{shortform_threads}",
            model_dir="models/whisper-large-v3-ct2",
            beam_size=1,
            cpu_threads=shortform_threads,
            without_timestamps=True,
            vad_filter=False,
        ),
        RunConfig(
            name=f"whisper_large_v3_turbo_beam1_nots_t{throughput_threads}",
            model_dir="models/whisper-large-v3-turbo-ct2",
            beam_size=1,
            cpu_threads=throughput_threads,
            without_timestamps=True,
            vad_filter=False,
        ),
        RunConfig(
            name=f"distil_large_v3_5_beam1_nots_t{shortform_threads}",
            model_dir="models/distil-large-v3.5-ct2",
            beam_size=1,
            cpu_threads=shortform_threads,
            without_timestamps=True,
            vad_filter=False,
        ),
    ]


PRESET_BUILDERS: dict[str, tuple[str, callable]] = {
    "default": (
        "Current curated sweep for the bundled distil-medium defaults and nearby tuning checks.",
        default_configs,
    ),
    "accuracy-bakeoff": (
        "Direct large-model comparison: whisper-large-v3 vs whisper-large-v3-turbo vs distil-large-v3.5.",
        accuracy_bakeoff_configs,
    ),
}


def list_presets() -> None:
    print("Available presets:")
    for name, (description, builder) in PRESET_BUILDERS.items():
        print(f"- {name}: {description}")
        for config in builder():
            print(f"    - {config.name}: {config.model_dir}")


def configs_for_preset(name: str) -> list[RunConfig]:
    entry = PRESET_BUILDERS.get(name)
    if entry is None:
        raise KeyError(name)
    return entry[1]()


def load_manifest(path: Path, limit: int) -> list[dict]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    return manifest[:limit]


def run_config(config: RunConfig, manifest: list[dict]) -> dict:
    runtime = resolve_runtime("cpu", "int8", config.cpu_threads)
    set_thread_env(runtime["cpu_threads"])

    model_path = PROJECT_ROOT / config.model_dir
    if not model_path.exists():
        raise FileNotFoundError(f"Model not found: {model_path}")

    load_start = time.perf_counter()
    model = load_whisper_model(
        model_path,
        device=runtime["device"],
        compute_type=runtime["compute_type"],
        cpu_threads=runtime["cpu_threads"],
    )
    model_load_s = time.perf_counter() - load_start

    sample_results = []
    decode_times = []
    short_decode_times = []
    rtfs = []
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
        if rtf is not None:
            rtfs.append(rtf)

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
        "total_audio_seconds": round(total_audio_s, 3),
        "total_decode_seconds": round(total_decode_s, 3),
        "results": sample_results,
    }


def format_bool(value: bool) -> str:
    return "yes" if value else "no"


def write_csv(path: Path, rows: list[dict]) -> None:
    fieldnames = [
        "rank",
        "name",
        "model_dir",
        "beam_size",
        "cpu_threads",
        "without_timestamps",
        "vad_filter",
        "avg_wer",
        "overall_rtf",
        "speed_x_realtime",
        "mean_decode_seconds",
        "median_decode_seconds",
        "p90_decode_seconds",
        "short_clip_mean_decode_seconds",
        "short_clip_mean_rtf",
        "model_load_seconds",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(path: Path, rows: list[dict]) -> None:
    lines = [
        "| rank | name | model | beam | threads | no_ts | vad | avg_wer | overall_rtf | short_clip_mean_s |",
        "| ---: | --- | --- | ---: | ---: | :---: | :---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            "| {rank} | {name} | {model_dir} | {beam_size} | {cpu_threads} | {without_timestamps} | "
            "{vad_filter} | {avg_wer:.4f} | {overall_rtf:.4f} | {short_clip_mean_decode_seconds:.3f} |".format(
                **row
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def leaderboard_rows(run_payloads: list[dict]) -> list[dict]:
    sorted_runs = sorted(
        run_payloads,
        key=lambda payload: (
            payload["avg_wer"],
            payload["short_clip_mean_decode_seconds"],
            payload["overall_rtf"],
        ),
    )

    rows = []
    for rank, payload in enumerate(sorted_runs, start=1):
        config = payload["config"]
        rows.append(
            {
                "rank": rank,
                "name": config["name"],
                "model_dir": config["model_dir"],
                "beam_size": config["beam_size"],
                "cpu_threads": config["cpu_threads"],
                "without_timestamps": format_bool(config["without_timestamps"]),
                "vad_filter": format_bool(config["vad_filter"]),
                "avg_wer": payload["avg_wer"],
                "overall_rtf": payload["overall_rtf"],
                "speed_x_realtime": payload["speed_x_realtime"],
                "mean_decode_seconds": payload["mean_decode_seconds"],
                "median_decode_seconds": payload["median_decode_seconds"],
                "p90_decode_seconds": payload["p90_decode_seconds"],
                "short_clip_mean_decode_seconds": payload["short_clip_mean_decode_seconds"],
                "short_clip_mean_rtf": payload["short_clip_mean_rtf"],
                "model_load_seconds": payload["model_load_seconds"],
            }
        )
    return rows


def print_leaderboard(rows: list[dict]) -> None:
    print()
    print("rank  avg_wer  rtf    short_s  beam  thr  no_ts  vad  model  name")
    print("-" * 100)
    for row in rows:
        print(
            f"{row['rank']:>4}  "
            f"{row['avg_wer']:.4f}  "
            f"{row['overall_rtf']:.3f}  "
            f"{row['short_clip_mean_decode_seconds']:.3f}  "
            f"{row['beam_size']:>4}  "
            f"{row['cpu_threads']:>3}  "
            f"{row['without_timestamps']:>5}  "
            f"{row['vad_filter']:>3}  "
            f"{Path(row['model_dir']).name:<27}  "
            f"{row['name']}"
        )


def main() -> int:
    args = parse_args()
    if args.list_presets:
        list_presets()
        return 0

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

    try:
        configs = configs_for_preset(args.preset)
    except KeyError:
        print(f"Unknown preset: {args.preset}", file=sys.stderr)
        print("Use --list-presets to inspect supported presets.", file=sys.stderr)
        return 1

    if args.config_name:
        requested = set(args.config_name)
        configs = [config for config in configs if config.name in requested]
        missing = sorted(requested - {config.name for config in configs})
        if missing:
            print(f"Unknown config name(s): {', '.join(missing)}", file=sys.stderr)
            return 1

    run_payloads = []
    for config in configs:
        print(f"Running {config.name}...", flush=True)
        payload = run_config(config, manifest)
        run_payloads.append(payload)
        out_path = output_dir / f"{config.name}.json"
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(
            f"  avg_wer={payload['avg_wer']:.4f} "
            f"overall_rtf={payload['overall_rtf']:.3f} "
            f"short_clip_mean_s={payload['short_clip_mean_decode_seconds']:.3f}",
            flush=True,
        )

    rows = leaderboard_rows(run_payloads)
    summary_payload = {
        "manifest": str(manifest_path),
        "samples": len(manifest),
        "preset": args.preset,
        "output_dir": str(output_dir),
        "leaderboard": rows,
        "runs": run_payloads,
    }

    (output_dir / "summary.json").write_text(json.dumps(summary_payload, indent=2), encoding="utf-8")
    write_csv(output_dir / "leaderboard.csv", rows)
    write_markdown(output_dir / "leaderboard.md", rows)
    print_leaderboard(rows)
    print(f"\nResults written to {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
