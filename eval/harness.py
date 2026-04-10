"""Shared evaluation harness for local model bakeoffs."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import textwrap
import time
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence
import re

from kdictate.app_metadata import DEFAULT_MODEL_NAME
from kdictate.audio_common import load_whisper_model
from kdictate.runtime_profile import (
    recommended_cpu_threads,
    recommended_shortform_cpu_threads,
    resolve_runtime,
    set_thread_env,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = PROJECT_ROOT / "eval" / "audio" / "manifest.json"
DEFAULT_SWEEP_RESULTS_ROOT = PROJECT_ROOT / "eval" / "results" / "sweeps"
DEFAULT_VERBOSE_RESULTS_ROOT = PROJECT_ROOT / "eval" / "results" / "verbose_benchmarks"
DEFAULT_SINGLE_RESULTS_ROOT = PROJECT_ROOT / "eval" / "results"
INSTALLED_MODELS_ROOT = Path.home() / ".local" / "share" / "kdictate" / "models"
SHORT_CLIP_THRESHOLD_SECONDS = 4.0
RULE_WIDTH = 120
PrintFn = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class EvalRunConfig:
    """Runtime settings for one eval run."""

    name: str
    model_dir: str
    cpu_threads: int
    beam_size: int = 1
    language: str = "en"
    task: str = "transcribe"
    compute_type: str | None = "int8"
    without_timestamps: bool = True
    vad_filter: bool = False
    condition_on_previous_text: bool = False
    best_of: int = 1
    temperature: float = 0.0
    no_speech_threshold: float = 0.6


def normalize_text(text: str) -> str:
    """Normalize text for WER-style comparisons."""

    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"(?<=\w)-(?=\w)", "", text)
    text = text.replace("'", "")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def percentile(values: Sequence[float], pct: float) -> float | None:
    """Return a linear-interpolated percentile for a non-empty sequence."""

    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = pct * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def summarize_numeric(values: Sequence[float]) -> dict[str, float | int | None]:
    """Return common descriptive stats for a numeric sequence."""

    if not values:
        return {
            "count": 0,
            "mean": None,
            "median": None,
            "p90": None,
            "minimum": None,
            "maximum": None,
        }
    return {
        "count": len(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "p90": percentile(values, 0.9),
        "minimum": min(values),
        "maximum": max(values),
    }


def _rounded(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None else None


def _metric_text(value: float | None, digits: int) -> str:
    return f"{value:.{digits}f}" if value is not None else "n/a"


def align_words(reference: str, hypothesis: str) -> dict[str, object]:
    """Return a token-level alignment suitable for WER and console diffs."""

    ref_tokens = normalize_text(reference).split()
    hyp_tokens = normalize_text(hypothesis).split()
    rows: list[dict[str, str]] = []

    if not ref_tokens and not hyp_tokens:
        return {
            "reference_tokens": ref_tokens,
            "hypothesis_tokens": hyp_tokens,
            "rows": rows,
            "counts": {
                "matches": 0,
                "substitutions": 0,
                "insertions": 0,
                "deletions": 0,
            },
            "wer": 0.0,
        }

    dp = [[0] * (len(hyp_tokens) + 1) for _ in range(len(ref_tokens) + 1)]
    for i in range(len(ref_tokens) + 1):
        dp[i][0] = i
    for j in range(len(hyp_tokens) + 1):
        dp[0][j] = j

    for i, ref_token in enumerate(ref_tokens, start=1):
        for j, hyp_token in enumerate(hyp_tokens, start=1):
            substitution_cost = 0 if ref_token == hyp_token else 1
            dp[i][j] = min(
                dp[i - 1][j] + 1,
                dp[i][j - 1] + 1,
                dp[i - 1][j - 1] + substitution_cost,
            )

    i = len(ref_tokens)
    j = len(hyp_tokens)
    matches = 0
    substitutions = 0
    insertions = 0
    deletions = 0

    while i > 0 or j > 0:
        if i > 0 and j > 0:
            substitution_cost = 0 if ref_tokens[i - 1] == hyp_tokens[j - 1] else 1
            if dp[i][j] == dp[i - 1][j - 1] + substitution_cost:
                if substitution_cost == 0:
                    rows.append({"op": "=", "reference": ref_tokens[i - 1], "hypothesis": hyp_tokens[j - 1]})
                    matches += 1
                else:
                    rows.append({"op": "~", "reference": ref_tokens[i - 1], "hypothesis": hyp_tokens[j - 1]})
                    substitutions += 1
                i -= 1
                j -= 1
                continue
        if i > 0 and dp[i][j] == dp[i - 1][j] + 1:
            rows.append({"op": "-", "reference": ref_tokens[i - 1], "hypothesis": ""})
            deletions += 1
            i -= 1
            continue
        rows.append({"op": "+", "reference": "", "hypothesis": hyp_tokens[j - 1]})
        insertions += 1
        j -= 1

    rows.reverse()
    denominator = len(ref_tokens)
    wer = ((substitutions + insertions + deletions) / denominator) if denominator else (1.0 if hyp_tokens else 0.0)
    return {
        "reference_tokens": ref_tokens,
        "hypothesis_tokens": hyp_tokens,
        "rows": rows,
        "counts": {
            "matches": matches,
            "substitutions": substitutions,
            "insertions": insertions,
            "deletions": deletions,
        },
        "wer": wer,
    }


def format_alignment_lines(rows: Sequence[dict[str, str]]) -> list[str]:
    """Format a token alignment table for console output."""

    ref_width = max(12, min(28, max((len(row["reference"]) for row in rows), default=12)))
    hyp_width = max(12, min(28, max((len(row["hypothesis"]) for row in rows), default=12)))
    lines = [
        f"  {'op':<2} {'reference':<{ref_width}} | {'hypothesis':<{hyp_width}}",
        f"  {'--':<2} {'-' * ref_width} | {'-' * hyp_width}",
    ]
    for row in rows:
        lines.append(f"  {row['op']:<2} {row['reference']:<{ref_width}} | {row['hypothesis']:<{hyp_width}}")
    return lines


def wrapped_block(text: str, *, indent: str = "  ", width: int = RULE_WIDTH) -> list[str]:
    """Wrap one text block for readable console output."""

    wrapped = textwrap.wrap(text, width=max(20, width - len(indent))) or [""]
    return [f"{indent}{line}" for line in wrapped]


def extract_info(info: object) -> dict[str, object]:
    """Extract stable transcription metadata from faster-whisper's info object."""

    payload: dict[str, object] = {}
    for attr in ("language", "language_probability", "duration", "duration_after_vad"):
        value = getattr(info, attr, None)
        if isinstance(value, float):
            payload[attr] = round(value, 5)
        elif value is not None:
            payload[attr] = value
    return payload


def model_path_candidates(
    model_dir: str | Path,
    *,
    project_root: Path = PROJECT_ROOT,
    installed_models_root: Path = INSTALLED_MODELS_ROOT,
) -> list[Path]:
    """Return plausible model locations in priority order."""

    raw = Path(model_dir).expanduser()
    candidates: list[Path] = []

    def add(candidate: Path) -> None:
        if candidate not in candidates:
            candidates.append(candidate)

    if raw.is_absolute():
        add(raw)
        return candidates

    add(project_root / raw)
    add(installed_models_root / raw)
    add(project_root / "models" / raw.name)
    add(installed_models_root / raw.name)
    return candidates


def resolve_model_path(
    model_dir: str | Path,
    *,
    project_root: Path = PROJECT_ROOT,
    installed_models_root: Path = INSTALLED_MODELS_ROOT,
) -> Path:
    """Resolve a model dir across repo-local and installed-runtime layouts."""

    candidates = model_path_candidates(
        model_dir,
        project_root=project_root,
        installed_models_root=installed_models_root,
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    checked = ", ".join(str(candidate) for candidate in candidates)
    raise FileNotFoundError(f"Model not found: {model_dir}. Checked: {checked}")


def load_manifest(path: Path, limit: int) -> list[dict[str, object]]:
    """Load the local eval manifest and absolutize relative audio paths."""

    manifest = json.loads(path.read_text(encoding="utf-8"))
    for item in manifest:
        audio_path = Path(item["path"])
        if not audio_path.is_absolute():
            item["path"] = str((PROJECT_ROOT / audio_path).resolve())
    return manifest[:limit]


def default_configs() -> list[EvalRunConfig]:
    """The older distil-medium tuning sweep kept for compatibility."""

    full_threads = recommended_cpu_threads()
    half_threads = max(1, full_threads // 2)
    return [
        EvalRunConfig(
            name=f"distil_medium_beam1_nots_t{full_threads}",
            model_dir="distil-medium-en-ct2-int8",
            beam_size=1,
            cpu_threads=full_threads,
        ),
        EvalRunConfig(
            name=f"distil_medium_beam1_nots_t{half_threads}",
            model_dir="distil-medium-en-ct2-int8",
            beam_size=1,
            cpu_threads=half_threads,
        ),
        EvalRunConfig(
            name=f"distil_medium_beam5_nots_t{full_threads}",
            model_dir="distil-medium-en-ct2-int8",
            beam_size=5,
            cpu_threads=full_threads,
        ),
        EvalRunConfig(
            name=f"distil_medium_beam1_nots_vad_t{full_threads}",
            model_dir="distil-medium-en-ct2-int8",
            beam_size=1,
            cpu_threads=full_threads,
            vad_filter=True,
        ),
        EvalRunConfig(
            name=f"distil_medium_beam5_nots_vad_t{half_threads}",
            model_dir="distil-medium-en-ct2-int8",
            beam_size=5,
            cpu_threads=half_threads,
            vad_filter=True,
        ),
    ]


def accuracy_bakeoff_configs() -> list[EvalRunConfig]:
    """Current large-model comparison set."""

    shortform_threads = recommended_shortform_cpu_threads()
    throughput_threads = recommended_cpu_threads()
    return [
        EvalRunConfig(
            name=f"whisper_large_v3_beam1_nots_t{shortform_threads}",
            model_dir="whisper-large-v3-ct2",
            beam_size=1,
            cpu_threads=shortform_threads,
        ),
        EvalRunConfig(
            name=f"whisper_large_v3_turbo_beam1_nots_t{throughput_threads}",
            model_dir="whisper-large-v3-turbo-ct2",
            beam_size=1,
            cpu_threads=throughput_threads,
        ),
        EvalRunConfig(
            name=f"distil_large_v3_5_beam1_nots_t{shortform_threads}",
            model_dir="distil-large-v3.5-ct2",
            beam_size=1,
            cpu_threads=shortform_threads,
        ),
    ]


PRESET_BUILDERS: dict[str, tuple[str, Callable[[], list[EvalRunConfig]]]] = {
    "default": (
        "Compatibility sweep around the older distil-medium defaults.",
        default_configs,
    ),
    "accuracy-bakeoff": (
        "Direct comparison of whisper-large-v3, whisper-large-v3-turbo, and distil-large-v3.5.",
        accuracy_bakeoff_configs,
    ),
    "current-models": (
        "Alias for the current large-model accuracy bakeoff.",
        accuracy_bakeoff_configs,
    ),
}


def list_presets(print_fn: PrintFn = print) -> None:
    """Print preset names and config membership."""

    print_fn("Available presets:")
    for name, (description, builder) in PRESET_BUILDERS.items():
        print_fn(f"- {name}: {description}")
        for config in builder():
            print_fn(f"    - {config.name}: {config.model_dir}")


def configs_for_preset(name: str) -> list[EvalRunConfig]:
    """Return the config list for a named preset."""

    entry = PRESET_BUILDERS.get(name)
    if entry is None:
        raise KeyError(name)
    return entry[1]()


def print_rule(title: str, print_fn: PrintFn = print) -> None:
    """Print a consistent console section divider."""

    print_fn("")
    print_fn("=" * RULE_WIDTH)
    print_fn(title)
    print_fn("=" * RULE_WIDTH)


def _print_run_header(
    config: EvalRunConfig,
    runtime: dict[str, object],
    model_path: Path,
    *,
    print_fn: PrintFn,
) -> None:
    print_rule(f"MODEL {config.name}", print_fn)
    print_fn(f"model_label={config.model_dir}")
    print_fn(f"model_path={model_path}")
    print_fn(
        "runtime="
        f"device={runtime['device']} "
        f"compute_type={runtime['compute_type']} "
        f"cpu_threads={runtime['cpu_threads']} "
        f"task={config.task} "
        f"beam_size={config.beam_size} "
        f"best_of={config.best_of} "
        f"temperature={config.temperature:.1f}"
    )
    print_fn(
        "decode_flags="
        f"without_timestamps={config.without_timestamps} "
        f"vad_filter={config.vad_filter} "
        f"condition_on_previous_text={config.condition_on_previous_text} "
        f"no_speech_threshold={config.no_speech_threshold}"
    )


def _print_sample_report(
    sample: dict[str, object],
    *,
    index: int,
    total: int,
    show_segments: bool,
    print_fn: PrintFn,
) -> None:
    print_fn("")
    print_fn(f"[{index}/{total}] sample_id={sample['id']} path={sample['path']}")
    print_fn(
        "metrics="
        f"audio_seconds={sample['audio_seconds']:.3f} "
        f"decode_seconds={sample['decode_seconds']:.3f} "
        f"rtf={(sample['real_time_factor'] if sample['real_time_factor'] is not None else float('nan')):.5f} "
        f"speed_x={(sample['speed_x_realtime'] if sample['speed_x_realtime'] is not None else float('nan')):.3f} "
        f"wer={sample['wer']:.5f} "
        f"matches={sample['matches']} "
        f"substitutions={sample['substitutions']} "
        f"insertions={sample['insertions']} "
        f"deletions={sample['deletions']} "
        f"segments={sample['segment_count']}"
    )
    info = sample["transcription_info"]
    if info:
        print_fn(f"transcription_info={json.dumps(info, sort_keys=True)}")

    print_fn("reference:")
    for line in wrapped_block(str(sample["reference"])):
        print_fn(line)
    print_fn("hypothesis:")
    for line in wrapped_block(str(sample["hypothesis"])):
        print_fn(line)
    print_fn("alignment:")
    for line in format_alignment_lines(sample["alignment"]["rows"]):
        print_fn(line)

    if show_segments:
        print_fn("segments:")
        print_fn("  #   start    end   elapsed  text")
        print_fn("  --  ------  ------ -------  ----")
        for segment in sample["segments"]:
            print_fn(
                f"  {segment['index']:>2}  "
                f"{segment['start']:>6.2f}  "
                f"{segment['end']:>6.2f}  "
                f"{segment['elapsed_seconds']:>7.2f}  "
                f"{segment['text']}"
            )


def _build_run_payload(
    *,
    config: EvalRunConfig,
    runtime: dict[str, object],
    model_path: Path,
    model_load_seconds: float,
    sample_results: list[dict[str, object]],
) -> dict[str, object]:
    decode_values = [float(sample["decode_seconds"]) for sample in sample_results]
    audio_values = [float(sample["audio_seconds"]) for sample in sample_results]
    rtf_values = [float(sample["real_time_factor"]) for sample in sample_results if sample["real_time_factor"] is not None]
    wer_values = [float(sample["wer"]) for sample in sample_results]
    segment_counts = [int(sample["segment_count"]) for sample in sample_results]
    language_confidences = [
        float(sample["transcription_info"]["language_probability"])
        for sample in sample_results
        if sample["transcription_info"].get("language_probability") is not None
    ]
    short_samples = [sample for sample in sample_results if float(sample["audio_seconds"]) <= SHORT_CLIP_THRESHOLD_SECONDS]
    short_decode_values = [float(sample["decode_seconds"]) for sample in short_samples]
    short_rtf_values = [float(sample["real_time_factor"]) for sample in short_samples if sample["real_time_factor"] is not None]

    total_audio_seconds = sum(audio_values)
    total_decode_seconds = sum(decode_values)
    overall_rtf = (total_decode_seconds / total_audio_seconds) if total_audio_seconds > 0 else None

    decode_stats = summarize_numeric(decode_values)
    audio_stats = summarize_numeric(audio_values)
    rtf_stats = summarize_numeric(rtf_values)
    wer_stats = summarize_numeric(wer_values)
    segment_stats = summarize_numeric(segment_counts)
    confidence_stats = summarize_numeric(language_confidences)
    short_decode_stats = summarize_numeric(short_decode_values)
    short_rtf_stats = summarize_numeric(short_rtf_values)

    total_reference_words = sum(int(sample["reference_word_count"]) for sample in sample_results)
    total_hypothesis_words = sum(int(sample["hypothesis_word_count"]) for sample in sample_results)
    error_counts = {
        "matches": sum(int(sample["matches"]) for sample in sample_results),
        "substitutions": sum(int(sample["substitutions"]) for sample in sample_results),
        "insertions": sum(int(sample["insertions"]) for sample in sample_results),
        "deletions": sum(int(sample["deletions"]) for sample in sample_results),
    }
    exact_match_count = sum(1 for sample in sample_results if sample["exact_match"])

    return {
        "config": asdict(config),
        "runtime": runtime,
        "model_path": str(model_path),
        "model_load_seconds": round(model_load_seconds, 3),
        "samples": len(sample_results),
        "avg_wer": _rounded(wer_stats["mean"], 5),
        "median_wer": _rounded(wer_stats["median"], 5),
        "p90_wer": _rounded(wer_stats["p90"], 5),
        "overall_rtf": _rounded(overall_rtf, 5),
        "mean_rtf": _rounded(rtf_stats["mean"], 5),
        "median_rtf": _rounded(rtf_stats["median"], 5),
        "p90_rtf": _rounded(rtf_stats["p90"], 5),
        "speed_x_realtime": _rounded((1.0 / overall_rtf), 3) if overall_rtf not in (None, 0) else None,
        "mean_decode_seconds": _rounded(decode_stats["mean"], 3),
        "median_decode_seconds": _rounded(decode_stats["median"], 3),
        "p90_decode_seconds": _rounded(decode_stats["p90"], 3),
        "min_decode_seconds": _rounded(decode_stats["minimum"], 3),
        "max_decode_seconds": _rounded(decode_stats["maximum"], 3),
        "mean_audio_seconds": _rounded(audio_stats["mean"], 3),
        "median_audio_seconds": _rounded(audio_stats["median"], 3),
        "p90_audio_seconds": _rounded(audio_stats["p90"], 3),
        "mean_segment_count": _rounded(segment_stats["mean"], 3),
        "median_segment_count": _rounded(segment_stats["median"], 3),
        "mean_language_confidence": _rounded(confidence_stats["mean"], 5),
        "short_clip_count": len(short_samples),
        "short_clip_mean_decode_seconds": _rounded(short_decode_stats["mean"], 3),
        "short_clip_median_decode_seconds": _rounded(short_decode_stats["median"], 3),
        "short_clip_mean_rtf": _rounded(short_rtf_stats["mean"], 5),
        "short_clip_median_rtf": _rounded(short_rtf_stats["median"], 5),
        "total_audio_seconds": round(total_audio_seconds, 3),
        "total_decode_seconds": round(total_decode_seconds, 3),
        "total_reference_words": total_reference_words,
        "total_hypothesis_words": total_hypothesis_words,
        "error_counts": error_counts,
        "exact_match_count": exact_match_count,
        "results": sample_results,
    }


def print_run_summary(payload: dict[str, object], *, print_fn: PrintFn = print) -> None:
    """Print a verbose per-model summary."""

    print_rule(f"SUMMARY {payload['config']['name']}", print_fn)
    print_fn(f"model_path={payload['model_path']}")
    print_fn(
        "aggregate="
        f"samples={payload['samples']} "
        f"avg_wer={_metric_text(payload['avg_wer'], 5)} "
        f"median_wer={_metric_text(payload['median_wer'], 5)} "
        f"p90_wer={_metric_text(payload['p90_wer'], 5)} "
        f"overall_rtf={_metric_text(payload['overall_rtf'], 5)} "
        f"speed_x={_metric_text(payload['speed_x_realtime'], 3)}"
    )
    print_fn(
        "decode_seconds="
        f"mean={_metric_text(payload['mean_decode_seconds'], 3)} "
        f"median={_metric_text(payload['median_decode_seconds'], 3)} "
        f"p90={_metric_text(payload['p90_decode_seconds'], 3)} "
        f"min={_metric_text(payload['min_decode_seconds'], 3)} "
        f"max={_metric_text(payload['max_decode_seconds'], 3)}"
    )
    print_fn(
        "short_clips="
        f"count={payload['short_clip_count']} "
        f"mean_decode_seconds={_metric_text(payload['short_clip_mean_decode_seconds'], 3)} "
        f"median_decode_seconds={_metric_text(payload['short_clip_median_decode_seconds'], 3)} "
        f"mean_rtf={_metric_text(payload['short_clip_mean_rtf'], 5)} "
        f"median_rtf={_metric_text(payload['short_clip_median_rtf'], 5)}"
    )
    print_fn(
        "errors="
        f"matches={payload['error_counts']['matches']} "
        f"substitutions={payload['error_counts']['substitutions']} "
        f"insertions={payload['error_counts']['insertions']} "
        f"deletions={payload['error_counts']['deletions']} "
        f"exact_matches={payload['exact_match_count']}"
    )
    print_fn(
        "totals="
        f"model_load_seconds={_metric_text(payload['model_load_seconds'], 3)} "
        f"audio_seconds={_metric_text(payload['total_audio_seconds'], 3)} "
        f"decode_seconds={_metric_text(payload['total_decode_seconds'], 3)} "
        f"reference_words={payload['total_reference_words']} "
        f"hypothesis_words={payload['total_hypothesis_words']}"
    )


def run_eval_config(
    config: EvalRunConfig,
    manifest: Sequence[dict[str, object]],
    *,
    show_segments: bool = True,
    print_fn: PrintFn = print,
) -> dict[str, object]:
    """Run one config and emit a verbose console transcript."""

    runtime = resolve_runtime("cpu", config.compute_type, config.cpu_threads)
    set_thread_env(runtime["cpu_threads"])
    model_path = resolve_model_path(config.model_dir)

    _print_run_header(config, runtime, model_path, print_fn=print_fn)

    load_start = time.perf_counter()
    model = load_whisper_model(
        model_path,
        device=runtime["device"],
        compute_type=runtime["compute_type"],
        cpu_threads=runtime["cpu_threads"],
    )
    model_load_seconds = time.perf_counter() - load_start
    print_fn(f"model_load_seconds={model_load_seconds:.3f}")

    sample_results: list[dict[str, object]] = []
    for index, item in enumerate(manifest, start=1):
        sample_start = time.perf_counter()
        segments, info = model.transcribe(
            item["path"],
            language=config.language,
            task=config.task,
            beam_size=config.beam_size,
            best_of=config.best_of,
            temperature=config.temperature,
            condition_on_previous_text=config.condition_on_previous_text,
            no_speech_threshold=config.no_speech_threshold,
            without_timestamps=config.without_timestamps,
            vad_filter=config.vad_filter,
        )

        text_parts: list[str] = []
        segment_payloads: list[dict[str, object]] = []
        audio_seconds = 0.0
        for segment_index, segment in enumerate(segments, start=1):
            clean_text = segment.text.strip()
            if clean_text:
                text_parts.append(clean_text)
            audio_seconds = max(audio_seconds, float(segment.end))
            segment_payloads.append(
                {
                    "index": segment_index,
                    "start": round(float(segment.start), 3),
                    "end": round(float(segment.end), 3),
                    "elapsed_seconds": round(time.perf_counter() - sample_start, 3),
                    "text": clean_text,
                }
            )

        info_payload = extract_info(info)
        if audio_seconds <= 0.0 and isinstance(info_payload.get("duration_after_vad"), float):
            audio_seconds = float(info_payload["duration_after_vad"])
        if audio_seconds <= 0.0 and isinstance(info_payload.get("duration"), float):
            audio_seconds = float(info_payload["duration"])

        decode_seconds = time.perf_counter() - sample_start
        hypothesis = " ".join(text_parts).strip()
        alignment = align_words(str(item["reference"]), hypothesis)
        rtf = (decode_seconds / audio_seconds) if audio_seconds > 0 else None

        sample_payload = {
            "id": item["id"],
            "path": item["path"],
            "reference": item["reference"],
            "reference_normalized": normalize_text(str(item["reference"])),
            "hypothesis": hypothesis,
            "hypothesis_normalized": normalize_text(hypothesis),
            "transcription_info": info_payload,
            "audio_seconds": round(audio_seconds, 3),
            "decode_seconds": round(decode_seconds, 3),
            "real_time_factor": _rounded(rtf, 5),
            "speed_x_realtime": _rounded((1.0 / rtf), 3) if rtf not in (None, 0) else None,
            "wer": round(float(alignment["wer"]), 5),
            "reference_word_count": len(alignment["reference_tokens"]),
            "hypothesis_word_count": len(alignment["hypothesis_tokens"]),
            "matches": alignment["counts"]["matches"],
            "substitutions": alignment["counts"]["substitutions"],
            "insertions": alignment["counts"]["insertions"],
            "deletions": alignment["counts"]["deletions"],
            "exact_match": alignment["counts"]["substitutions"] == 0
            and alignment["counts"]["insertions"] == 0
            and alignment["counts"]["deletions"] == 0,
            "segment_count": len(segment_payloads),
            "segments": segment_payloads,
            "alignment": {
                "rows": alignment["rows"],
                "counts": alignment["counts"],
            },
        }
        sample_results.append(sample_payload)
        _print_sample_report(
            sample_payload,
            index=index,
            total=len(manifest),
            show_segments=show_segments,
            print_fn=print_fn,
        )

    payload = _build_run_payload(
        config=config,
        runtime=runtime,
        model_path=model_path,
        model_load_seconds=model_load_seconds,
        sample_results=sample_results,
    )
    print_run_summary(payload, print_fn=print_fn)
    return payload


def leaderboard_rows(run_payloads: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    """Sort run payloads into a compact leaderboard."""

    ordered = sorted(
        run_payloads,
        key=lambda payload: (
            payload["avg_wer"],
            payload["overall_rtf"],
            payload["mean_decode_seconds"],
        ),
    )
    rows: list[dict[str, object]] = []
    for rank, payload in enumerate(ordered, start=1):
        config = payload["config"]
        rows.append(
            {
                "rank": rank,
                "name": config["name"],
                "model_dir": config["model_dir"],
                "beam_size": config["beam_size"],
                "cpu_threads": config["cpu_threads"],
                "without_timestamps": "yes" if config["without_timestamps"] else "no",
                "vad_filter": "yes" if config["vad_filter"] else "no",
                "avg_wer": payload["avg_wer"],
                "median_wer": payload["median_wer"],
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


def print_leaderboard(rows: Sequence[dict[str, object]], *, print_fn: PrintFn = print) -> None:
    """Print the final compact ranking table."""

    print_rule("FINAL LEADERBOARD", print_fn)
    print_fn("rank  avg_wer  rtf     speed_x  mean_s  p90_s  beam  thr  no_ts  vad  model")
    print_fn("-" * RULE_WIDTH)
    for row in rows:
        print_fn(
            f"{row['rank']:>4}  "
            f"{_metric_text(row['avg_wer'], 5):>7}  "
            f"{_metric_text(row['overall_rtf'], 5):>7}  "
            f"{_metric_text(row['speed_x_realtime'], 3):>7}  "
            f"{_metric_text(row['mean_decode_seconds'], 3):>6}  "
            f"{_metric_text(row['p90_decode_seconds'], 3):>5}  "
            f"{row['beam_size']:>4}  "
            f"{row['cpu_threads']:>3}  "
            f"{row['without_timestamps']:>5}  "
            f"{row['vad_filter']:>3}  "
            f"{Path(str(row['model_dir'])).name}"
        )


def write_csv(path: Path, rows: Sequence[dict[str, object]]) -> None:
    """Write leaderboard rows to CSV."""

    fieldnames = [
        "rank",
        "name",
        "model_dir",
        "beam_size",
        "cpu_threads",
        "without_timestamps",
        "vad_filter",
        "avg_wer",
        "median_wer",
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


def write_leaderboard_markdown(path: Path, rows: Sequence[dict[str, object]]) -> None:
    """Write the compact leaderboard as markdown."""

    lines = [
        "| rank | name | model | beam | threads | no_ts | vad | avg_wer | rtf | mean_s | p90_s |",
        "| ---: | --- | --- | ---: | ---: | :---: | :---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            "| {rank} | {name} | {model_dir} | {beam_size} | {cpu_threads} | {without_timestamps} | "
            "{vad_filter} | {avg_wer:.5f} | {overall_rtf:.5f} | {mean_decode_seconds:.3f} | "
            "{p90_decode_seconds:.3f} |".format(**row)
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_run_markdown_report(path: Path, payload: dict[str, object]) -> None:
    """Write a verbose human-readable report for one run."""

    config = payload["config"]
    lines = [
        f"# Eval Report: {config['name']}",
        "",
        f"- Model label: `{config['model_dir']}`",
        f"- Model path: `{payload['model_path']}`",
        f"- Avg WER: `{payload['avg_wer']:.5f}`",
        f"- Median WER: `{payload['median_wer']:.5f}`",
        f"- Overall RTF: `{payload['overall_rtf']:.5f}`",
        f"- Mean decode seconds: `{payload['mean_decode_seconds']:.3f}`",
        f"- Model load seconds: `{payload['model_load_seconds']:.3f}`",
        "",
        "## Samples",
        "",
    ]
    for sample in payload["results"]:
        lines.extend(
            [
                f"### Sample {sample['id']}",
                "",
                f"- Path: `{sample['path']}`",
                f"- Audio seconds: `{sample['audio_seconds']:.3f}`",
                f"- Decode seconds: `{sample['decode_seconds']:.3f}`",
                f"- RTF: `{sample['real_time_factor']:.5f}`" if sample["real_time_factor"] is not None else "- RTF: `n/a`",
                f"- WER: `{sample['wer']:.5f}`",
                f"- Matches/Substitutions/Insertions/Deletions: `{sample['matches']}/{sample['substitutions']}/{sample['insertions']}/{sample['deletions']}`",
                "",
                "**Reference**",
                "",
                sample["reference"],
                "",
                "**Hypothesis**",
                "",
                sample["hypothesis"] or "(empty)",
                "",
                "| op | reference | hypothesis |",
                "| :-- | :-- | :-- |",
            ]
        )
        for row in sample["alignment"]["rows"]:
            lines.append(f"| {row['op']} | {row['reference']} | {row['hypothesis']} |")
        lines.extend(
            [
                "",
                "| # | start | end | elapsed | text |",
                "| ---: | ---: | ---: | ---: | :-- |",
            ]
        )
        for segment in sample["segments"]:
            lines.append(
                f"| {segment['index']} | {segment['start']:.3f} | {segment['end']:.3f} | "
                f"{segment['elapsed_seconds']:.3f} | {segment['text']} |"
            )
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def create_output_dir(results_root: Path, tag: str | None) -> Path:
    """Create and return the timestamped output directory."""

    results_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir_name = f"{timestamp}_{tag}" if tag else timestamp
    output_dir = results_root / output_dir_name
    output_dir.mkdir(parents=True, exist_ok=False)
    return output_dir


def build_sweep_arg_parser(
    *,
    description: str,
    default_results_root: Path,
    default_preset: str,
) -> argparse.ArgumentParser:
    """Create the shared multi-config eval parser."""

    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="Path to the eval manifest JSON.")
    parser.add_argument("--samples", type=int, default=20, help="Number of manifest entries to evaluate.")
    parser.add_argument(
        "--results-root",
        default=str(default_results_root),
        help="Directory where timestamped results are written.",
    )
    parser.add_argument("--tag", default=None, help="Optional suffix for the timestamped output directory.")
    parser.add_argument(
        "--preset",
        default=default_preset,
        help="Named config preset to run. Use --list-presets to inspect available presets.",
    )
    parser.add_argument("--list-presets", action="store_true", help="Print presets and exit.")
    parser.add_argument(
        "--config-name",
        action="append",
        default=[],
        help="Run only the named config(s). May be provided multiple times.",
    )
    parser.add_argument(
        "--skip-missing-models",
        action="store_true",
        help="Skip missing model directories instead of exiting.",
    )
    parser.add_argument(
        "--hide-segments",
        action="store_true",
        help="Suppress the per-segment section while keeping full transcript diffs.",
    )
    return parser


def run_sweep_from_namespace(args: argparse.Namespace, *, print_fn: PrintFn = print) -> int:
    """Execute a multi-config eval run from parsed args."""

    if args.list_presets:
        list_presets(print_fn)
        return 0

    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        raise SystemExit(f"Manifest not found: {manifest_path}")
    manifest = load_manifest(manifest_path, args.samples)
    if not manifest:
        raise SystemExit("Manifest is empty.")

    try:
        configs = configs_for_preset(args.preset)
    except KeyError as exc:
        raise SystemExit(f"Unknown preset: {args.preset}") from exc

    if args.config_name:
        requested = set(args.config_name)
        configs = [config for config in configs if config.name in requested]
        missing = sorted(requested - {config.name for config in configs})
        if missing:
            raise SystemExit(f"Unknown config name(s): {', '.join(missing)}")

    runnable_configs: list[EvalRunConfig] = []
    for config in configs:
        try:
            resolve_model_path(config.model_dir)
            runnable_configs.append(config)
        except FileNotFoundError:
            if args.skip_missing_models:
                print_fn(f"Skipping missing model_dir={config.model_dir}")
                continue
            raise

    if not runnable_configs:
        raise SystemExit("No runnable model configs remain.")

    output_dir = create_output_dir(Path(args.results_root), args.tag)
    print_rule("EVAL SWEEP", print_fn)
    print_fn(f"manifest={manifest_path}")
    print_fn(f"samples={len(manifest)}")
    print_fn(f"preset={args.preset}")
    print_fn(f"output_dir={output_dir}")
    print_fn("configs=" + ", ".join(config.name for config in runnable_configs))

    run_payloads = []
    for config in runnable_configs:
        payload = run_eval_config(
            config,
            manifest,
            show_segments=not args.hide_segments,
            print_fn=print_fn,
        )
        run_payloads.append(payload)
        json_path = output_dir / f"{config.name}.json"
        report_path = output_dir / f"{config.name}.md"
        json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        write_run_markdown_report(report_path, payload)

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
    write_csv(output_dir / "leaderboard.csv", rows)
    write_leaderboard_markdown(output_dir / "leaderboard.md", rows)
    print_leaderboard(rows, print_fn=print_fn)
    print_fn(f"results_written_to={output_dir}")
    return 0


def build_single_arg_parser(*, description: str, default_results_root: Path) -> argparse.ArgumentParser:
    """Create the single-model verbose eval parser."""

    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="Path to the eval manifest JSON.")
    parser.add_argument("--samples", type=int, default=20, help="Number of manifest entries to evaluate.")
    parser.add_argument(
        "--results-root",
        default=str(default_results_root),
        help="Directory where timestamped results are written.",
    )
    parser.add_argument("--tag", default=None, help="Optional output tag.")
    parser.add_argument("--model-dir", required=False, default=DEFAULT_MODEL_NAME)
    parser.add_argument("--beam-size", type=int, default=1, help="Whisper beam size.")
    parser.add_argument("--language", default="en")
    parser.add_argument("--task", default="transcribe", choices=("transcribe", "translate"))
    parser.add_argument("--cpu-threads", type=int, default=recommended_shortform_cpu_threads())
    parser.add_argument("--compute-type", default="int8", choices=("float32", "float16", "int8", "int8_float16"))
    parser.add_argument("--best-of", type=int, default=1)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--vad-filter", action="store_true", default=False)
    parser.add_argument("--condition-on-previous", action="store_true", default=False)
    parser.add_argument("--with-timestamps", action="store_true", default=False)
    parser.add_argument("--no-speech-threshold", type=float, default=0.6)
    parser.add_argument("--hide-segments", action="store_true", help="Suppress the per-segment section.")
    return parser


def run_single_from_namespace(args: argparse.Namespace, *, print_fn: PrintFn = print) -> int:
    """Execute a single-model verbose eval run."""

    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        raise SystemExit(f"Manifest not found: {manifest_path}")
    manifest = load_manifest(manifest_path, args.samples)
    if not manifest:
        raise SystemExit("Manifest is empty.")

    resolve_model_path(args.model_dir)
    name_bits = [
        Path(args.model_dir).name.replace(".", "_"),
        f"beam{args.beam_size}",
        f"t{args.cpu_threads}",
    ]
    if args.vad_filter:
        name_bits.append("vad")
    if args.condition_on_previous:
        name_bits.append("prev")
    if args.tag:
        name_bits.append(args.tag)

    config = EvalRunConfig(
        name="_".join(name_bits),
        model_dir=args.model_dir,
        cpu_threads=args.cpu_threads,
        beam_size=args.beam_size,
        language=args.language,
        task=args.task,
        compute_type=args.compute_type,
        without_timestamps=not args.with_timestamps,
        vad_filter=args.vad_filter,
        condition_on_previous_text=args.condition_on_previous,
        best_of=args.best_of,
        temperature=args.temperature,
        no_speech_threshold=args.no_speech_threshold,
    )

    output_dir = create_output_dir(Path(args.results_root), args.tag)
    payload = run_eval_config(
        config,
        manifest,
        show_segments=not args.hide_segments,
        print_fn=print_fn,
    )
    (output_dir / f"{config.name}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    write_run_markdown_report(output_dir / f"{config.name}.md", payload)
    print_fn(f"results_written_to={output_dir}")
    return 0
