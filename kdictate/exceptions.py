"""Domain exceptions for kdictate."""

from __future__ import annotations


class KDictateError(RuntimeError):
    """Base class for controlled kdictate failures."""


class ConfigurationError(KDictateError):
    """Raised when configuration is invalid or incomplete."""


class DbusServiceError(KDictateError):
    """Raised when the session D-Bus service cannot start or respond."""


class IbusEngineError(KDictateError):
    """Raised when the IBus frontend cannot initialize or operate cleanly."""


class AudioInputError(KDictateError):
    """Raised when microphone capture cannot start or continue."""


class TranscriptionError(KDictateError):
    """Raised when Whisper decode fails in a controlled way."""


class FocusContextError(KDictateError):
    """Raised when the IBus engine cannot safely commit text to a focus target."""
