"""Audio-device helpers for the dictation core."""

from __future__ import annotations

import subprocess
from typing import Final

DEFAULT_PACTL_TIMEOUT_S: Final[float] = 3.0


def resolve_default_input_device() -> tuple[str, bool]:
    """Return ``(description, usable)`` for the default PulseAudio/PipeWire source."""

    try:
        result = subprocess.run(
            ["pactl", "get-default-source"],
            capture_output=True,
            text=True,
            timeout=DEFAULT_PACTL_TIMEOUT_S,
        )
        source_name = result.stdout.strip()
    except Exception:  # noqa: BLE001
        return ("unknown", False)

    if not source_name:
        return ("none", False)
    if source_name.endswith(".monitor"):
        return (source_name, False)

    try:
        result = subprocess.run(
            ["pactl", "list", "sources"],
            capture_output=True,
            text=True,
            timeout=DEFAULT_PACTL_TIMEOUT_S,
        )
        in_target = False
        for line in result.stdout.splitlines():
            stripped = line.strip()
            parts = stripped.split(None, 1)
            if stripped.startswith("Name:") and len(parts) > 1 and parts[1] == source_name:
                in_target = True
            elif in_target and stripped.startswith("Description:"):
                return (stripped.split(":", 1)[1].strip(), True)
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("Failed to resolve input device description: %s", exc)

    return (source_name, True)
