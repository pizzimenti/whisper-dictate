"""Pure HUD state reducer -- no UI or D-Bus dependencies."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum, auto
from typing import Union

from kdictate.constants import (
    STATE_ERROR,
    STATE_IDLE,
    STATE_RECORDING,
    STATE_STARTING,
    STATE_TRANSCRIBING,
)


class HudPhase(Enum):
    """Visual phase of the HUD overlay."""

    HIDDEN = auto()
    STARTING = auto()
    LISTENING = auto()
    PARTIAL = auto()
    TRANSCRIBING = auto()
    COMMITTING = auto()
    ERROR = auto()


@dataclass(frozen=True, slots=True)
class HudModel:
    """Immutable snapshot of HUD presentation state."""

    phase: HudPhase = HudPhase.HIDDEN
    partial_text: str = ""
    commit_text: str = ""
    error_message: str = ""
    daemon_available: bool = False


# -- Events ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DaemonAppeared:
    """Daemon bus name appeared on session bus."""


@dataclass(frozen=True, slots=True)
class DaemonVanished:
    """Daemon bus name disappeared."""


@dataclass(frozen=True, slots=True)
class DaemonStateChanged:
    """Daemon emitted a StateChanged signal."""

    state: str


@dataclass(frozen=True, slots=True)
class PartialTranscript:
    """Daemon emitted a PartialTranscript signal."""

    text: str


@dataclass(frozen=True, slots=True)
class FinalTranscript:
    """Daemon emitted a FinalTranscript signal."""

    text: str


@dataclass(frozen=True, slots=True)
class ErrorOccurred:
    """Daemon emitted an ErrorOccurred signal."""

    code: str
    message: str


@dataclass(frozen=True, slots=True)
class CommitAckExpired:
    """The brief commit acknowledgment display period elapsed."""


@dataclass(frozen=True, slots=True)
class SnapshotReceived:
    """Coarse daemon snapshot for restart-tolerant seeding."""

    state: str
    active_partial: str
    last_final: str
    error_code: str
    error_message: str


HudEvent = Union[
    DaemonAppeared,
    DaemonVanished,
    DaemonStateChanged,
    PartialTranscript,
    FinalTranscript,
    ErrorOccurred,
    CommitAckExpired,
    SnapshotReceived,
]


# -- Reducer ---------------------------------------------------------------


def reduce(model: HudModel, event: HudEvent) -> HudModel:
    """Pure state transition -- returns a new model without side effects."""

    if isinstance(event, DaemonAppeared):
        return replace(model, daemon_available=True)

    if isinstance(event, DaemonVanished):
        return HudModel()

    if isinstance(event, DaemonStateChanged):
        return _apply_daemon_state(model, event.state)

    if isinstance(event, PartialTranscript):
        return _apply_partial(model, event.text)

    if isinstance(event, FinalTranscript):
        return _apply_final(model, event.text)

    if isinstance(event, ErrorOccurred):
        return replace(
            model,
            phase=HudPhase.ERROR,
            partial_text="",
            commit_text="",
            error_message=event.message,
        )

    if isinstance(event, CommitAckExpired):
        if model.phase == HudPhase.COMMITTING:
            return replace(model, phase=HudPhase.HIDDEN, commit_text="",
                           error_message="")
        return model

    if isinstance(event, SnapshotReceived):
        return _apply_snapshot(model, event)

    return model


# -- Internal helpers ------------------------------------------------------


def _apply_daemon_state(model: HudModel, daemon_state: str) -> HudModel:
    if daemon_state == STATE_IDLE:
        if model.phase == HudPhase.COMMITTING:
            return model
        return replace(model, phase=HudPhase.HIDDEN, partial_text="",
                       commit_text="", error_message="")

    if daemon_state == STATE_STARTING:
        return replace(model, phase=HudPhase.STARTING, partial_text="",
                       commit_text="", error_message="")

    if daemon_state == STATE_RECORDING:
        # Preserve an already-visible partial so a redundant state signal
        # does not flash the HUD back to "Listening..." mid-utterance.
        if model.phase == HudPhase.PARTIAL and model.partial_text:
            return model
        return replace(model, phase=HudPhase.LISTENING, partial_text="",
                       commit_text="", error_message="")

    if daemon_state == STATE_TRANSCRIBING:
        return replace(model, phase=HudPhase.TRANSCRIBING,
                       commit_text="", error_message="")

    if daemon_state == STATE_ERROR:
        return replace(
            model,
            phase=HudPhase.ERROR,
            partial_text="",
            error_message="Daemon reported an error",
        )

    return replace(
        model,
        phase=HudPhase.ERROR,
        partial_text="",
        error_message=f"Unknown daemon state: {daemon_state}",
    )


def _apply_partial(model: HudModel, text: str) -> HudModel:
    if model.phase not in (HudPhase.LISTENING, HudPhase.PARTIAL, HudPhase.TRANSCRIBING):
        return model

    stripped = text.strip()
    if stripped:
        return replace(model, phase=HudPhase.PARTIAL, partial_text=stripped)
    return replace(model, phase=HudPhase.LISTENING, partial_text="")


def _apply_final(model: HudModel, text: str) -> HudModel:
    stripped = text.strip()
    if stripped:
        return replace(
            model,
            phase=HudPhase.COMMITTING,
            partial_text="",
            commit_text=stripped,
            error_message="",
        )
    return replace(model, partial_text="")


def _apply_snapshot(model: HudModel, snap: SnapshotReceived) -> HudModel:
    """Seed the full HUD model from a daemon snapshot."""

    # Start from the daemon state to get the right phase.
    base = _apply_daemon_state(model, snap.state)

    # Layer on the partial text if the phase accepts it.
    partial = snap.active_partial.strip()
    if partial and base.phase in (HudPhase.LISTENING, HudPhase.PARTIAL, HudPhase.TRANSCRIBING):
        base = replace(base, phase=HudPhase.PARTIAL, partial_text=partial)

    # Layer on error details if the daemon is in error state.
    if base.phase == HudPhase.ERROR and snap.error_message:
        base = replace(base, error_message=snap.error_message)

    return base
