"""Shared constants for the IBus-only whisper-dictate redesign."""

from __future__ import annotations

from typing import Final

APP_ROOT_ID: Final[str] = "io.github.pizzimenti.WhisperDictate"
DBUS_BUS_NAME: Final[str] = f"{APP_ROOT_ID}1"
DBUS_OBJECT_PATH: Final[str] = "/io/github/pizzimenti/WhisperDictate1"
DBUS_INTERFACE: Final[str] = f"{APP_ROOT_ID}1"

STATE_IDLE: Final[str] = "idle"
STATE_RECORDING: Final[str] = "recording"
STATE_TRANSCRIBING: Final[str] = "transcribing"
STATE_ERROR: Final[str] = "error"

CANONICAL_STATES: Final[tuple[str, ...]] = (
    STATE_IDLE,
    STATE_RECORDING,
    STATE_TRANSCRIBING,
    STATE_ERROR,
)
