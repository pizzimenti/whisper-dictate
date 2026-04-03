"""Domain exceptions for whisper-dictate."""

from __future__ import annotations


class WhisperDictateError(RuntimeError):
    """Base class for controlled whisper-dictate failures."""


class ConfigurationError(WhisperDictateError):
    """Raised when configuration is invalid or incomplete."""


class DbusServiceError(WhisperDictateError):
    """Raised when the session D-Bus service cannot start or respond."""


class IbusEngineError(WhisperDictateError):
    """Raised when the IBus frontend cannot initialize or operate cleanly."""


class AudioInputError(WhisperDictateError):
    """Raised when microphone capture cannot start or continue."""


class TranscriptionError(WhisperDictateError):
    """Raised when Whisper decode fails in a controlled way."""


class FocusContextError(WhisperDictateError):
    """Raised when the IBus engine cannot safely commit text to a focus target."""
