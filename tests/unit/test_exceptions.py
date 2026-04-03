"""Exception hierarchy tests for the shared error types."""

from __future__ import annotations

import unittest

from whisper_dictate.exceptions import (
    AudioInputError,
    ConfigurationError,
    DbusServiceError,
    FocusContextError,
    IbusEngineError,
    TranscriptionError,
    WhisperDictateError,
)


class ExceptionHierarchyTests(unittest.TestCase):
    """Keep the domain exception tree shallow and predictable."""

    def test_domain_exceptions_share_common_base(self) -> None:
        for exc_type in (
            ConfigurationError,
            DbusServiceError,
            IbusEngineError,
            AudioInputError,
            TranscriptionError,
            FocusContextError,
        ):
            self.assertTrue(issubclass(exc_type, WhisperDictateError))
