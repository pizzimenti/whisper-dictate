"""Reusable test doubles for whisper-dictate."""

from .doubles import FakeDbusService, FakeIbusContext, TranscriptBridge

__all__ = ["FakeDbusService", "FakeIbusContext", "TranscriptBridge"]
