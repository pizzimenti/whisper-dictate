"""Audio-device helpers for the dictation core."""

from __future__ import annotations

import logging
import subprocess
from typing import Final

DEFAULT_PACTL_TIMEOUT_S: Final[float] = 3.0
_LOGGER = logging.getLogger(__name__)


def _run_pactl(*args: str) -> subprocess.CompletedProcess[str]:
    """Run pactl with UTF-8 decoding that survives odd locale settings."""

    return subprocess.run(
        ["pactl", *args],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=DEFAULT_PACTL_TIMEOUT_S,
    )


def resolve_default_input_device() -> tuple[str, bool]:
    """Return ``(description, usable)`` for the default PulseAudio/PipeWire source."""

    try:
        result = _run_pactl("get-default-source")
        source_name = result.stdout.strip()
    except Exception:  # noqa: BLE001
        return ("unknown", False)

    if not source_name:
        return ("none", False)

    # If the default is a monitor (speaker loopback), try to find a real
    # input device before giving up.
    if source_name.endswith(".monitor"):
        fallback = _find_first_real_input()
        if fallback is not None:
            name, description = fallback
            _LOGGER.info(
                "Default source %s is a monitor; switching to %s (%s)",
                source_name, name, description,
            )
            try:
                _run_pactl("set-default-source", name)
            except Exception:  # noqa: BLE001
                _LOGGER.warning("Failed to set default source to %s", name)
            return (description, True)
        return (source_name, False)

    return _describe_source(source_name)


def _describe_source(source_name: str) -> tuple[str, bool]:
    """Look up the human-readable description for a named source."""

    try:
        result = _run_pactl("list", "sources")
        in_target = False
        for line in result.stdout.splitlines():
            stripped = line.strip()
            parts = stripped.split(None, 1)
            if stripped.startswith("Name:") and len(parts) > 1 and parts[1] == source_name:
                in_target = True
            elif in_target and stripped.startswith("Description:"):
                return (stripped.split(":", 1)[1].strip(), True)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("Failed to resolve input device description: %s", exc)

    return (source_name, True)


def _find_first_real_input() -> tuple[str, str] | None:
    """Scan pactl sources for the first non-monitor input device.

    Returns ``(source_name, description)`` or ``None`` if no real input
    device exists.
    """

    try:
        result = _run_pactl("list", "sources", "short")
    except Exception:  # noqa: BLE001
        return None

    candidates: list[str] = []
    for line in result.stdout.strip().splitlines():
        fields = line.split("\t")
        if len(fields) >= 2:
            name = fields[1]
            if not name.endswith(".monitor"):
                candidates.append(name)

    if not candidates:
        return None

    # Return the first real input with its description.
    for name in candidates:
        desc, usable = _describe_source(name)
        if usable:
            return (name, desc)

    return None
