"""Tests for the IBus engine process entrypoint."""

from __future__ import annotations

from unittest import mock
import unittest

from whisper_dictate.ibus_engine import main as ibus_main


class IbusEngineMainTests(unittest.TestCase):
    def test_main_uses_sys_argv_when_no_override_is_provided(self) -> None:
        fake_ibus = mock.Mock()
        fake_ibus.init.return_value = None

        with (
            mock.patch.object(ibus_main, "load_ibus_module", return_value=fake_ibus),
            mock.patch.object(ibus_main, "initialize_engine_runtime", return_value=(object(), mock.Mock())) as init_runtime,
            mock.patch.object(ibus_main.GLib, "MainLoop") as main_loop_cls,
            mock.patch.object(ibus_main.sys, "argv", ["/tmp/ibus-engine-whisper-dictate"]),
        ):
            loop = main_loop_cls.return_value
            loop.run.side_effect = KeyboardInterrupt

            result = ibus_main.main()

        self.assertEqual(result, 0)
        init_runtime.assert_called_once_with("/tmp/ibus-engine-whisper-dictate", ibus_module=fake_ibus)
