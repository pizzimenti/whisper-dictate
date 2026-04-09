#!/usr/bin/env python3
"""Evaluate kdictate accuracy (WER) and speed (RTF) against LibriSpeech test-clean.

Downloads 20 samples from LibriSpeech, transcribes each, and reports:
- WER (Word Error Rate): lower is better, 0% = perfect
- RTF (Real-Time Factor): <1.0 means faster than real-time
- Per-sample breakdown saved to results/

Usage:
    cd kdictate
    .venv/bin/python eval/evaluate.py [--samples 20] [--beam-size 5] [--vad-filter]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Add parent dir so we can import the kdictate package
sys.path.insert(0, str(Path(__file__).parent.parent))
from kdictate.audio_common import load_whisper_model
from kdictate.runtime_profile import recommended_shortform_cpu_threads, resolve_runtime, set_thread_env

AUDIO_DIR = Path(__file__).parent / "audio"
RESULTS_DIR = Path(__file__).parent / "results"

# Import helper for disabling audio decode in datasets
try:
    from datasets.features import Audio as datasets_Audio
except ImportError:
    datasets_Audio = None


def download_samples(n: int) -> list[dict]:
    """Download n LibriSpeech test-clean samples, return list of {path, reference}."""
    import json as _json

    print(f"Downloading {n} LibriSpeech test-clean samples...", flush=True)

    # Download the manifest to get file paths and transcripts
    manifest_path = AUDIO_DIR / "manifest.json"
    if not manifest_path.exists():
        # Use the datasets API just for metadata, decode audio ourselves
        from datasets import load_dataset
        ds = load_dataset(
            "librispeech_asr", "clean", split="test", streaming=True,
        ).cast_column("audio", datasets_Audio(decode=False))

        manifest = []
        for i, item in enumerate(ds):
            if i >= n:
                break
            ref = item["text"].strip()
            audio_bytes = item["audio"]["bytes"]
            wav_path = AUDIO_DIR / f"librispeech_{i:03d}.flac"
            if not wav_path.exists():
                wav_path.write_bytes(audio_bytes)
            manifest.append({"path": str(wav_path), "reference": ref, "id": i})
            print(f"  [{i+1}/{n}] {ref[:60]}...", flush=True)
        manifest_path.write_text(_json.dumps(manifest, indent=2))
    else:
        manifest = _json.loads(manifest_path.read_text())
        manifest = manifest[:n]
        print(f"  Using cached manifest ({len(manifest)} samples)", flush=True)

    return manifest


def transcribe_sample(model, audio_path: str, language: str, beam_size: int,
                      vad_filter: bool, condition_on_previous: bool) -> tuple[str, float, float]:
    """Transcribe a single audio file. Returns (text, elapsed_s, audio_duration_s)."""
    start = time.perf_counter()
    segments, info = model.transcribe(
        audio_path,
        language=language,
        beam_size=beam_size,
        best_of=1,
        temperature=0.0,
        vad_filter=vad_filter,
        condition_on_previous_text=condition_on_previous,
        no_speech_threshold=0.6,
    )
    text_parts = []
    audio_duration = 0.0
    for seg in segments:
        text_parts.append(seg.text.strip())
        if seg.end > audio_duration:
            audio_duration = seg.end
    elapsed = time.perf_counter() - start
    text = " ".join(t for t in text_parts if t)
    return text, elapsed, audio_duration


def compute_wer(reference: str, hypothesis: str) -> float:
    from jiwer import wer
    if not reference.strip():
        return 0.0 if not hypothesis.strip() else 1.0
    return wer(reference.lower(), hypothesis.lower())


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Evaluate kdictate accuracy and speed.")
    p.add_argument("--samples", type=int, default=20, help="Number of LibriSpeech samples.")
    p.add_argument("--beam-size", type=int, default=5, help="Whisper beam size.")
    p.add_argument("--vad-filter", action="store_true", default=False, help="Enable VAD filter.")
    p.add_argument("--condition-on-previous", action="store_true", default=False)
    p.add_argument("--language", default="en")
    p.add_argument("--cpu-threads", type=int, default=recommended_shortform_cpu_threads())
    p.add_argument("--compute-type", default=None)
    p.add_argument("--model-dir", default=str(Path(__file__).parent.parent / "models/distil-medium-en-ct2-int8"))
    p.add_argument("--tag", default=None, help="Tag for this run (used in result filename).")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    runtime = resolve_runtime("cpu", args.compute_type, args.cpu_threads)
    set_thread_env(runtime["cpu_threads"])

    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    model_dir = Path(args.model_dir)
    if not model_dir.exists():
        print(f"Model not found: {model_dir}", file=sys.stderr)
        return 1

    # Download samples
    samples = download_samples(args.samples)

    # Load model
    print(f"\nLoading model from {model_dir}...", flush=True)
    load_start = time.perf_counter()
    model = load_whisper_model(
        model_dir,
        device=runtime["device"],
        compute_type=runtime["compute_type"],
        cpu_threads=runtime["cpu_threads"],
    )
    load_s = time.perf_counter() - load_start
    print(f"Model loaded in {load_s:.2f}s\n", flush=True)

    # Evaluate
    config = {
        "beam_size": args.beam_size,
        "vad_filter": args.vad_filter,
        "condition_on_previous_text": args.condition_on_previous,
        "language": args.language,
        "compute_type": runtime["compute_type"],
        "cpu_threads": runtime["cpu_threads"],
        "model_dir": str(model_dir),
    }

    results = []
    total_wer = 0.0
    total_rtf = 0.0
    total_audio_s = 0.0
    total_transcribe_s = 0.0

    print(f"{'#':>3}  {'WER':>6}  {'RTF':>6}  {'Audio':>6}  {'Time':>6}  Text")
    print("-" * 80)

    for sample in samples:
        hyp, elapsed, audio_dur = transcribe_sample(
            model, sample["path"], args.language, args.beam_size,
            args.vad_filter, args.condition_on_previous,
        )
        sample_wer = compute_wer(sample["reference"], hyp)
        rtf = elapsed / audio_dur if audio_dur > 0 else float("inf")

        total_audio_s += audio_dur
        total_transcribe_s += elapsed
        total_wer += sample_wer

        results.append({
            "id": sample["id"],
            "reference": sample["reference"],
            "hypothesis": hyp,
            "wer": round(sample_wer, 4),
            "rtf": round(rtf, 4),
            "audio_s": round(audio_dur, 2),
            "transcribe_s": round(elapsed, 2),
        })

        preview = hyp[:40] + "..." if len(hyp) > 40 else hyp
        print(f"{sample['id']:>3}  {sample_wer:>5.1%}  {rtf:>6.3f}  {audio_dur:>5.1f}s  {elapsed:>5.1f}s  {preview}")

    # Summary
    n = len(results)
    avg_wer = total_wer / n if n > 0 else 0
    overall_rtf = total_transcribe_s / total_audio_s if total_audio_s > 0 else 0
    speed_x = 1.0 / overall_rtf if overall_rtf > 0 else 0

    summary = {
        "config": config,
        "model_load_s": round(load_s, 2),
        "samples": n,
        "avg_wer": round(avg_wer, 4),
        "overall_rtf": round(overall_rtf, 4),
        "speed_x_realtime": round(speed_x, 2),
        "total_audio_s": round(total_audio_s, 2),
        "total_transcribe_s": round(total_transcribe_s, 2),
        "results": results,
    }

    tag = args.tag or f"beam{args.beam_size}_vad{'1' if args.vad_filter else '0'}"
    out_path = RESULTS_DIR / f"{tag}.json"
    out_path.write_text(json.dumps(summary, indent=2))

    print(f"\n{'='*80}")
    print(f"SUMMARY — {tag}")
    print(f"{'='*80}")
    print(f"  Samples:        {n}")
    print(f"  Avg WER:        {avg_wer:.1%}")
    print(f"  Overall RTF:    {overall_rtf:.3f}")
    print(f"  Speed:          {speed_x:.1f}x real-time")
    print(f"  Total audio:    {total_audio_s:.1f}s")
    print(f"  Total decode:   {total_transcribe_s:.1f}s")
    print(f"  Model load:     {load_s:.1f}s")
    print(f"  Results saved:  {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
