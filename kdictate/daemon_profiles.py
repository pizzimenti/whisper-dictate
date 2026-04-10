"""Named daemon argument profiles used by runtime and packaging entrypoints."""

from __future__ import annotations

from typing import Final

from kdictate.app_metadata import DEFAULT_MODEL_DIR
from kdictate.runtime_profile import recommended_shortform_cpu_threads

DEFAULT_DAEMON_PROFILE: Final[str] = "interactive"
SERVICE_DAEMON_PROFILE: Final[str] = "service"
DAEMON_PROFILE_CHOICES: Final[tuple[str, ...]] = (
    DEFAULT_DAEMON_PROFILE,
    SERVICE_DAEMON_PROFILE,
)


def daemon_arg_defaults(profile: str) -> dict[str, object]:
    """Return parser defaults for the requested daemon profile."""

    defaults: dict[str, object] = {
        "model_dir": str(DEFAULT_MODEL_DIR),
        "language": "en",
        "sample_rate": 16000,
        "beam_size": 1,
        "condition_on_previous_text": False,
        "vad_filter": False,
        "no_speech_threshold": 0.6,
        "cpu_threads": recommended_shortform_cpu_threads(),
        "compute_type": "int8",
        "block_ms": 30,
        "energy_threshold": 600.0,
        "silence_ms": 220,
        "min_speech_ms": 180,
        "start_speech_ms": 90,
        "max_utterance_s": 2.5,
    }
    if profile == DEFAULT_DAEMON_PROFILE:
        return defaults
    if profile == SERVICE_DAEMON_PROFILE:
        defaults.update(
            {
                "vad_filter": True,
                "silence_ms": 500,
                "max_utterance_s": 8.0,
            }
        )
        return defaults
    raise ValueError(f"Unsupported daemon profile: {profile}")
