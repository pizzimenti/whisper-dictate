"""Tests for the shared logging helper."""

from __future__ import annotations

import io
import logging
import unittest

from whisper_dictate.logging_utils import configure_logging


class LoggingUtilsTests(unittest.TestCase):
    """Verify logger setup is deterministic and does not duplicate handlers."""

    def test_configure_logging_installs_one_handler(self) -> None:
        stream = io.StringIO()
        logger_name = "whisper_dictate.tests.logging_utils"
        logger = logging.getLogger(logger_name)
        original_handlers = list(logger.handlers)
        for handler in original_handlers:
            logger.removeHandler(handler)

        try:
            configured = configure_logging(logger_name, stream=stream)
            self.assertIs(configured, logger)
            self.assertEqual(len(configured.handlers), 1)
            self.assertFalse(configured.propagate)

            configured.info("hello world")
            output = stream.getvalue()
            self.assertIn(logger_name, output)
            self.assertIn("hello world", output)

            configure_logging(logger_name, stream=stream)
            self.assertEqual(len(configured.handlers), 1)
        finally:
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
            for handler in original_handlers:
                logger.addHandler(handler)
