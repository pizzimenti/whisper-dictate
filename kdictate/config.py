"""Configuration helpers for the kdictate daemon."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from kdictate.daemon_profiles import (
    DAEMON_PROFILE_CHOICES,
    DEFAULT_DAEMON_PROFILE,
    daemon_arg_defaults,
)
from kdictate.runtime import RuntimePaths, default_runtime_paths


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


def build_arg_parser(*, profile: str = DEFAULT_DAEMON_PROFILE) -> argparse.ArgumentParser:
    """Create the daemon argument parser."""

    runtime_paths = default_runtime_paths()
    defaults = daemon_arg_defaults(profile)
    parser = argparse.ArgumentParser(
        description="KDictate daemon backed by session D-Bus."
    )
    parser.add_argument(
        "--profile",
        choices=DAEMON_PROFILE_CHOICES,
        default=profile,
        help="Named daemon tuning profile.",
    )
    parser.add_argument(
        "--model-dir",
        default=str(defaults["model_dir"]),
        help="Path to the CTranslate2 model directory.",
    )
    parser.add_argument("--language", default=defaults["language"], help="Language code for transcription.")
    parser.add_argument("--sample-rate", type=int, default=defaults["sample_rate"], help="Microphone sample rate.")
    parser.add_argument("--beam-size", type=int, default=defaults["beam_size"], help="Whisper beam size.")
    parser.add_argument(
        "--condition-on-previous-text",
        action=argparse.BooleanOptionalAction,
        default=defaults["condition_on_previous_text"],
        help="Condition on previous text between segments.",
    )
    parser.add_argument(
        "--vad-filter",
        action=argparse.BooleanOptionalAction,
        default=defaults["vad_filter"],
        help="Enable Whisper's built-in VAD filtering before decode.",
    )
    parser.add_argument(
        "--no-speech-threshold",
        type=float,
        default=defaults["no_speech_threshold"],
        help="Reject segments below this no-speech confidence threshold.",
    )
    parser.add_argument(
        "--cpu-threads",
        type=int,
        default=defaults["cpu_threads"],
        help="Override CPU thread count.",
    )
    parser.add_argument(
        "--compute-type",
        default=defaults["compute_type"],
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
        default=defaults["block_ms"],
        help="Audio capture block duration in milliseconds.",
    )
    parser.add_argument(
        "--energy-threshold",
        type=float,
        default=defaults["energy_threshold"],
        help="RMS threshold for speech detection.",
    )
    parser.add_argument(
        "--silence-ms",
        type=int,
        default=defaults["silence_ms"],
        help="Silence duration that commits the current utterance.",
    )
    parser.add_argument(
        "--min-speech-ms",
        type=int,
        default=defaults["min_speech_ms"],
        help="Minimum speech duration required to transcribe an utterance.",
    )
    parser.add_argument(
        "--start-speech-ms",
        type=int,
        default=defaults["start_speech_ms"],
        help="Consecutive voiced duration required before an utterance starts.",
    )
    parser.add_argument(
        "--max-utterance-s",
        type=float,
        default=defaults["max_utterance_s"],
        help="Force-commit an utterance when it reaches this length.",
    )
    return parser


def _parse_profile(argv: Sequence[str] | None = None) -> str:
    """Read the selected daemon profile before building the full parser."""

    bootstrap = argparse.ArgumentParser(add_help=False)
    bootstrap.add_argument(
        "--profile",
        choices=DAEMON_PROFILE_CHOICES,
        default=DEFAULT_DAEMON_PROFILE,
    )
    namespace, _ = bootstrap.parse_known_args(argv)
    return namespace.profile


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse daemon arguments."""

    profile = _parse_profile(argv)
    return build_arg_parser(profile=profile).parse_args(argv)
