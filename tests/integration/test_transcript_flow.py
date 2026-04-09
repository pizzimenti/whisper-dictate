"""Integration-style transcript flow tests built on test doubles."""

from __future__ import annotations

import io
import logging
import unittest

from tests.fixtures import FakeDbusService, FakeIbusContext, TranscriptBridge


class TranscriptFlowIntegrationTests(unittest.TestCase):
    """Exercise the partial-preedit / final-commit contract end to end."""

    def setUp(self) -> None:
        self.stream = io.StringIO()
        self.logger = logging.getLogger("kdictate.tests.integration")
        self.original_handlers = list(self.logger.handlers)
        for handler in self.original_handlers:
            self.logger.removeHandler(handler)
        self.logger.addHandler(logging.StreamHandler(self.stream))
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False

    def tearDown(self) -> None:
        for handler in list(self.logger.handlers):
            self.logger.removeHandler(handler)
        for handler in self.original_handlers:
            self.logger.addHandler(handler)

    def test_happy_path_partial_then_final_commit(self) -> None:
        service = FakeDbusService()
        context = FakeIbusContext()
        bridge = TranscriptBridge(service.bus, context, logger=self.logger)
        self.assertTrue(bridge.attach(service.bus))

        service.start()
        service.emit_partial("hello partial")
        service.emit_final("hello final")

        self.assertEqual(context.committed_text, ["hello final"])
        self.assertEqual(context.preedit_text, "")
        self.assertIn("partial transcript received: hello partial", self.stream.getvalue())
        self.assertIn("final transcript received: hello final", self.stream.getvalue())

    def test_focus_loss_blocks_final_commit(self) -> None:
        service = FakeDbusService()
        context = FakeIbusContext()
        bridge = TranscriptBridge(service.bus, context, logger=self.logger)
        bridge.attach(service.bus)

        service.emit_partial("draft")
        context.focus_out()
        service.emit_final("final should not commit")

        self.assertEqual(context.committed_text, [])
        self.assertEqual(context.preedit_text, "")
        self.assertIn("ignored_commit:final should not commit", context.event_log)

    def test_daemon_unavailable_is_noop_until_reconnected(self) -> None:
        context = FakeIbusContext()
        bridge = TranscriptBridge(None, context, logger=self.logger)

        self.assertFalse(bridge.attach(None))
        self.assertIn("daemon unavailable", self.stream.getvalue())

        service = FakeDbusService()
        self.assertTrue(bridge.attach(service.bus))
        service.emit_partial("reconnected")
        self.assertEqual(context.preedit_text, "reconnected")

    def test_idle_state_clears_stale_preedit(self) -> None:
        service = FakeDbusService()
        context = FakeIbusContext()
        bridge = TranscriptBridge(service.bus, context, logger=self.logger)
        bridge.attach(service.bus)

        service.emit_partial("stale")
        self.assertEqual(context.preedit_text, "stale")
        service.bus.emit("StateChanged", "idle")
        self.assertEqual(context.preedit_text, "")
