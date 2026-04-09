"""Shared runtime and control-plane helpers for kdictate."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from kdictate.constants import (
    STATE_ERROR,
    STATE_IDLE,
    STATE_RECORDING,
    STATE_STARTING,
    STATE_TRANSCRIBING,
)
from kdictate.exceptions import KDictateError


DEFAULT_STATE_POLL_INTERVAL_S = 0.15

STATE_MISSING = "missing"


class DaemonControlError(KDictateError):
    """Raised when a control helper cannot reach the running daemon."""


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    """Filesystem paths shared by the daemon and its helpers."""

    state_file: Path
    last_text_file: Path


def default_runtime_paths(*, uid: int | None = None) -> RuntimePaths:
    """Return the default XDG runtime file locations for the current user."""

    actual_uid = os.getuid() if uid is None else uid
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        runtime_dir = Path(xdg)
    else:
        # XDG_RUNTIME_DIR is always set on a live systemd desktop session, but
        # may be absent in minimal or headless environments.  /run/user/<uid>
        # is the canonical backing directory that systemd creates for each user
        # and is only accessible to that user (mode 700), so it is a safe
        # second choice.  Falling back to /tmp would expose state files to
        # other users on multi-user systems.
        fallback = Path(f"/run/user/{actual_uid}")
        if fallback.is_dir():
            runtime_dir = fallback
        else:
            raise RuntimeError(
                "XDG_RUNTIME_DIR is not set and /run/user/{uid} does not exist; "
                "cannot determine a safe runtime directory"
            )
    return RuntimePaths(
        state_file=runtime_dir / f"kdictate-{actual_uid}.state",
        last_text_file=runtime_dir / f"kdictate-{actual_uid}.last.txt",
    )


def read_state(state_file: Path) -> str:
    """Read the daemon state file, returning ``missing`` if it is absent or empty."""

    if not state_file.exists():
        return STATE_MISSING
    value = state_file.read_text(encoding="utf-8").strip()
    return value or STATE_MISSING


def write_state(state_file: Path, value: str) -> None:
    """Persist the daemon state in a newline-terminated runtime file.

    Atomic via write-then-rename so a crash mid-write cannot leave the
    state file truncated, empty, or holding a partially-written value
    like ``"rec"`` (instead of ``"recording"``) — which downstream
    helpers would treat as an unrecognized state and spin on.
    """

    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(state_file.suffix + ".tmp")
    tmp.write_text(f"{value}\n", encoding="utf-8")
    tmp.replace(state_file)


def read_last_text(last_text_file: Path) -> str:
    """Read the latest transcript, returning an empty string if none exists."""

    if not last_text_file.exists():
        return ""
    return last_text_file.read_text(encoding="utf-8")


def write_last_text(last_text_file: Path, value: str) -> None:
    """Persist the latest transcript text for control helpers."""

    last_text_file.parent.mkdir(parents=True, exist_ok=True)
    last_text_file.write_text(value, encoding="utf-8")


def wait_for_state(
    state_file: Path,
    targets: Iterable[str],
    timeout: float,
    *,
    poll_interval: float = DEFAULT_STATE_POLL_INTERVAL_S,
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
