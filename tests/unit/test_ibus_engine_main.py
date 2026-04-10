"""Tests for the IBus engine process entrypoint."""

from __future__ import annotations

import io
from unittest import mock
import unittest

from kdictate.ibus_engine import main as ibus_main
from kdictate.exceptions import IbusEngineError


class IbusEngineMainTests(unittest.TestCase):
    def test_main_uses_sys_argv_when_no_override_is_provided(self) -> None:
        fake_ibus = mock.Mock()
        fake_ibus.init.return_value = None

        with (
            mock.patch.object(ibus_main, "load_ibus_module", return_value=fake_ibus),
            mock.patch.object(ibus_main, "initialize_engine_runtime", return_value=(object(), mock.Mock())) as init_runtime,
            mock.patch.object(ibus_main.GLib, "MainLoop") as main_loop_cls,
            mock.patch.object(ibus_main.sys, "argv", ["/tmp/ibus-engine-kdictate"]),
        ):
            loop = main_loop_cls.return_value
            loop.run.side_effect = KeyboardInterrupt

            result = ibus_main.main()

        self.assertEqual(result, 0)
        init_runtime.assert_called_once_with("/tmp/ibus-engine-kdictate", ibus_module=fake_ibus)

    def test_main_returns_one_for_ibus_engine_error(self) -> None:
        stderr = io.StringIO()
        logger = mock.Mock()

        with (
            mock.patch.object(ibus_main, "configure_logging", return_value=logger),
            mock.patch.object(ibus_main, "_startup_engine_runtime", side_effect=IbusEngineError("bad ibus state")),
            mock.patch("sys.stderr", stderr),
        ):
            result = ibus_main.main(["/tmp/ibus-engine-kdictate"])

        self.assertEqual(result, 1)
        self.assertIn("bad ibus state", stderr.getvalue())

    def test_main_returns_one_for_unexpected_exception(self) -> None:
        stderr = io.StringIO()
        logger = mock.Mock()

        with (
            mock.patch.object(ibus_main, "configure_logging", return_value=logger),
            mock.patch.object(ibus_main, "_startup_engine_runtime", side_effect=RuntimeError("boom")),
            mock.patch("sys.stderr", stderr),
        ):
            result = ibus_main.main(["/tmp/ibus-engine-kdictate"])

        self.assertEqual(result, 1)
        self.assertIn("IBus engine startup failed: boom", stderr.getvalue())
