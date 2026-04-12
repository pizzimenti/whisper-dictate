"""Default daemon arguments."""

from __future__ import annotations

from kdictate.app_metadata import DEFAULT_MODEL_DIR
from kdictate.runtime_profile import recommended_shortform_cpu_threads


def daemon_arg_defaults() -> dict[str, object]:
    """Return the daemon argument defaults."""

    return {
        "model_dir": str(DEFAULT_MODEL_DIR),
        "language": "en",
        "sample_rate": 16000,
        "beam_size": 1,
        "condition_on_previous_text": False,
        "vad_filter": True,
        "no_speech_threshold": 0.6,
        "cpu_threads": recommended_shortform_cpu_threads(),
        "compute_type": "int8",
        "block_ms": 30,
        "energy_threshold": 1500.0,
        "silence_ms": 300,
        "min_speech_ms": 180,
        "start_speech_ms": 150,
        "max_utterance_s": 10.0,
    }
