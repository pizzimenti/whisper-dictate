"""Configuration helpers for the whisper-dictate daemon."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from whisper_dictate.runtime import RuntimePaths, default_runtime_paths
from whisper_dictate.runtime_profile import recommended_shortform_cpu_threads


DEFAULT_MODEL_DIR = Path(__file__).resolve().parent.parent / "models/whisper-large-v3-turbo-ct2"


@dataclass(frozen=True, slots=True)
class DictationConfig:
    """Validated configuration for the dictation core."""

    model_dir: Path
    language: str
    sample_rate: int
    beam_size: int
    condition_on_previous_text: bool
    vad_filter: bool
    no_speech_threshold: float
    cpu_threads: int
    compute_type: str
    block_ms: int
    energy_threshold: float
    silence_ms: int
    min_speech_ms: int
    start_speech_ms: int
    max_utterance_s: float
    runtime_paths: RuntimePaths

    @classmethod
    def from_namespace(cls, namespace: argparse.Namespace) -> "DictationConfig":
        """Create a config object from parsed CLI arguments."""

        runtime_paths = RuntimePaths(
            state_file=Path(namespace.state_file),
            last_text_file=Path(namespace.last_text_file),
        )
        return cls(
            model_dir=Path(namespace.model_dir),
            language=namespace.language,
            sample_rate=namespace.sample_rate,
            beam_size=namespace.beam_size,
            condition_on_previous_text=namespace.condition_on_previous_text,
            vad_filter=namespace.vad_filter,
            no_speech_threshold=namespace.no_speech_threshold,
            cpu_threads=namespace.cpu_threads,
            compute_type=namespace.compute_type,
            block_ms=namespace.block_ms,
            energy_threshold=namespace.energy_threshold,
            silence_ms=namespace.silence_ms,
            min_speech_ms=namespace.min_speech_ms,
            start_speech_ms=namespace.start_speech_ms,
            max_utterance_s=namespace.max_utterance_s,
            runtime_paths=runtime_paths,
        )


def build_arg_parser() -> argparse.ArgumentParser:
    """Create the daemon argument parser."""

    runtime_paths = default_runtime_paths()
    parser = argparse.ArgumentParser(
        description="Whisper-Dictate daemon backed by session D-Bus."
    )
    parser.add_argument(
        "--model-dir",
        default=str(DEFAULT_MODEL_DIR),
        help="Path to the CTranslate2 model directory.",
    )
    parser.add_argument("--language", default="en", help="Language code for transcription.")
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
        help="Enable Whisper's built-in VAD filtering before decode.",
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
        help="Override CPU thread count.",
    )
    parser.add_argument(
        "--compute-type",
        default="int8",
        choices=("float32", "float16", "int8", "int8_float16"),
        help="Compute type used by faster-whisper.",
    )
    parser.add_argument(
        "--state-file",
        default=str(runtime_paths.state_file),
        help="Runtime state cache path.",
    )
    parser.add_argument(
        "--last-text-file",
        default=str(runtime_paths.last_text_file),
        help="Runtime transcript cache path.",
    )
    parser.add_argument(
        "--block-ms",
        type=int,
        default=30,
        help="Audio capture block duration in milliseconds.",
    )
    parser.add_argument(
        "--energy-threshold",
        type=float,
        default=600.0,
        help="RMS threshold for speech detection.",
    )
    parser.add_argument(
        "--silence-ms",
        type=int,
        default=220,
        help="Silence duration that commits the current utterance.",
    )
    parser.add_argument(
        "--min-speech-ms",
        type=int,
        default=180,
        help="Minimum speech duration required to transcribe an utterance.",
    )
    parser.add_argument(
        "--start-speech-ms",
        type=int,
        default=90,
        help="Consecutive voiced duration required before an utterance starts.",
    )
    parser.add_argument(
        "--max-utterance-s",
        type=float,
        default=2.5,
        help="Force-commit an utterance when it reaches this length.",
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse daemon arguments."""

    return build_arg_parser().parse_args(argv)
