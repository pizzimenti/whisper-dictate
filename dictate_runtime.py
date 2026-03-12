"""Shared runtime and control-plane helpers for whisper-dictate.

This module is intentionally stdlib-only so it can be imported both by the
virtualenv-backed daemon and by system-Python helpers such as the Wayland
hotkey listener.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DAEMON_PGREP_PATTERN = r"python.*dictate\.py"

STATE_IDLE = "idle"
STATE_RECORDING = "recording"
STATE_TRANSCRIBING = "transcribing"
STATE_MISSING = "missing"


class DaemonControlError(RuntimeError):
    """Raised when a control helper cannot reach the running daemon."""


@dataclass(frozen=True)
class RuntimePaths:
    """Filesystem paths shared by the daemon and its control helpers."""

    state_file: Path
    last_text_file: Path


def default_runtime_paths(*, uid: int | None = None) -> RuntimePaths:
    """Return the default XDG runtime file locations for the current user."""

    actual_uid = os.getuid() if uid is None else uid
    runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
    return RuntimePaths(
        state_file=runtime_dir / f"whisper-dictate-{actual_uid}.state",
        last_text_file=runtime_dir / f"whisper-dictate-{actual_uid}.last.txt",
    )


def read_state(state_file: Path) -> str:
    """Read the daemon state file, returning ``missing`` if it is absent or empty."""

    if not state_file.exists():
        return STATE_MISSING
    value = state_file.read_text(encoding="utf-8").strip()
    return value or STATE_MISSING


def write_state(state_file: Path, value: str) -> None:
    """Persist the daemon state in a newline-terminated runtime file."""

    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(f"{value}\n", encoding="utf-8")


def read_last_text(last_text_file: Path) -> str:
    """Read the latest transcript, returning an empty string if none exists."""

    if not last_text_file.exists():
        return ""
    return last_text_file.read_text(encoding="utf-8")


def write_last_text(last_text_file: Path, value: str) -> None:
    """Persist the latest transcript text for control helpers."""

    last_text_file.parent.mkdir(parents=True, exist_ok=True)
    last_text_file.write_text(value, encoding="utf-8")


def daemon_pid(*, pattern: str = DAEMON_PGREP_PATTERN) -> int | None:
    """Return the first running whisper-dictate daemon PID, if any."""

    result = subprocess.run(
        ["pgrep", "-f", pattern],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line:
            return int(line)
    return None


def signal_daemon(sig: signal.Signals, *, pattern: str = DAEMON_PGREP_PATTERN) -> int:
    """Send a UNIX signal to the running whisper-dictate daemon."""

    pid = daemon_pid(pattern=pattern)
    if pid is None:
        raise DaemonControlError("whisper-dictate daemon is not running")
    os.kill(pid, sig)
    return pid


def wait_for_state(
    state_file: Path,
    targets: Iterable[str],
    timeout: float,
    *,
    poll_interval: float = 0.05,
) -> str | None:
    """Wait for the daemon state file to reach one of ``targets``."""

    target_states = set(targets)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = read_state(state_file)
        if state in target_states:
            return state
        time.sleep(poll_interval)

    state = read_state(state_file)
    if state in target_states:
        return state
    return None
