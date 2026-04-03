from __future__ import annotations

from types import SimpleNamespace
import unittest

from whisper_dictate.ibus_engine.engine import is_toggle_shortcut


class IbusShortcutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ibus = SimpleNamespace(
            KEY_space=32,
            ModifierType=SimpleNamespace(
                CONTROL_MASK=4,
                RELEASE_MASK=1 << 30,
            ),
        )

    def test_ctrl_space_toggles(self) -> None:
        self.assertTrue(is_toggle_shortcut(32, 4, self.ibus))

    def test_non_space_does_not_toggle(self) -> None:
        self.assertFalse(is_toggle_shortcut(13, 4, self.ibus))

    def test_key_release_does_not_toggle(self) -> None:
        self.assertFalse(is_toggle_shortcut(32, 4 | (1 << 30), self.ibus))

    def test_space_without_control_does_not_toggle(self) -> None:
        self.assertFalse(is_toggle_shortcut(32, 0, self.ibus))
