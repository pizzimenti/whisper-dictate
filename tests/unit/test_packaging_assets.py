"""Tests for the installed packaging and service assets."""

from __future__ import annotations

from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

from kdictate.constants import DBUS_INTERFACE
from kdictate.ibus_engine.engine import ENGINE_NAME


class PackagingAssetTests(unittest.TestCase):
    """Validate that the IBus packaging metadata stays internally consistent."""

    def test_component_metadata_matches_root_identity(self) -> None:
        component_path = Path("packaging/io.github.pizzimenti.KDictate.component.xml")
        root = ET.fromstring(component_path.read_text(encoding="utf-8"))

        self.assertEqual(root.findtext("name"), "io.github.pizzimenti.KDictate")
        self.assertEqual(root.findtext("exec"), "@@ENGINE_EXEC@@")
        self.assertEqual(root.findtext("version"), "0.3")
        self.assertEqual(root.findtext("textdomain"), "kdictate")

        engine = root.find("engines/engine")
        self.assertIsNotNone(engine)
        assert engine is not None
        self.assertEqual(engine.findtext("name"), DBUS_INTERFACE)
        self.assertEqual(ENGINE_NAME, DBUS_INTERFACE)
        self.assertEqual(engine.findtext("longname"), "KDictate")
        self.assertEqual(engine.findtext("language"), "en")
        self.assertEqual(engine.findtext("license"), "MIT")
        self.assertEqual(engine.findtext("icon"), "audio-input-microphone")
        self.assertEqual(engine.findtext("version"), "0.3")
        self.assertEqual(engine.findtext("textdomain"), "kdictate")
        self.assertEqual(engine.findtext("rank"), "1")

    def test_dbus_and_systemd_service_files_reference_the_frozen_identity(self) -> None:
        dbus_service_path = Path("packaging/io.github.pizzimenti.KDictate.service")
        systemd_service_path = Path("systemd/io.github.pizzimenti.KDictate.service")
        launcher_template_path = Path("packaging/ibus-engine-kdictate.sh")
        toggle_desktop_path = Path("packaging/io.github.pizzimenti.KDictateToggle.desktop")
        env_template_path = Path("packaging/60-kdictate-ibus.conf")
        plasma_env_script_path = Path("packaging/kdictate-plasma-wayland.sh")

        dbus_service = dbus_service_path.read_text(encoding="utf-8")
        systemd_service = systemd_service_path.read_text(encoding="utf-8")
        launcher_template = launcher_template_path.read_text(encoding="utf-8")
        toggle_desktop = toggle_desktop_path.read_text(encoding="utf-8")
        env_template = env_template_path.read_text(encoding="utf-8")
        plasma_env_script = plasma_env_script_path.read_text(encoding="utf-8")

        self.assertIn("Name=io.github.pizzimenti.KDictate1", dbus_service)
        self.assertIn("Exec=", dbus_service)
        self.assertIn("SystemdService=io.github.pizzimenti.KDictate.service", dbus_service)
        self.assertIn("io.github.pizzimenti.KDictate.service", systemd_service_path.name)
        self.assertIn("ExecStart=", systemd_service)
        self.assertIn("-m kdictate.core", dbus_service)
        self.assertIn("-m kdictate.core", systemd_service)
        self.assertNotIn("--no-type-output", dbus_service)
        self.assertNotIn("--no-type-output", systemd_service)
        self.assertTrue(launcher_template_path.exists())
        self.assertIn("-m kdictate.ibus_engine", launcher_template)
        self.assertTrue(toggle_desktop_path.exists())
        self.assertIn("gdbus call", toggle_desktop)
        self.assertIn("io.github.pizzimenti.KDictate1.Toggle", toggle_desktop)
        self.assertIn("X-KDE-Shortcuts=Ctrl+Space", toggle_desktop)
        self.assertTrue(env_template_path.exists())
        self.assertIn("IBUS_COMPONENT_PATH=", env_template)
        self.assertIn("@@HOME@@/.local/share/ibus/component", env_template)
        self.assertIn("${IBUS_COMPONENT_PATH:+:$IBUS_COMPONENT_PATH}", env_template)
        self.assertIn("XMODIFIERS=@im=ibus", env_template)
        self.assertNotIn("GTK_IM_MODULE=ibus", env_template)
        self.assertNotIn("QT_IM_MODULE=ibus", env_template)
        self.assertTrue(plasma_env_script_path.exists())
        self.assertIn("unset GTK_IM_MODULE", plasma_env_script)
        self.assertIn("unset QT_IM_MODULE", plasma_env_script)

    def test_regression_shell_check_is_executable_and_scoped(self) -> None:
        script_path = Path("scripts/check-ibus-only.sh")
        self.assertTrue(script_path.exists())
        script = script_path.read_text(encoding="utf-8")

        self.assertTrue(script.startswith("#!/usr/bin/env bash"))
        self.assertIn("systemd", script)
        self.assertIn("packaging", script)
        self.assertIn("kdictate", script)
        self.assertIn("ydotool|dotool|wtype|wl-copy|xdotool|type_text", script)

    def test_install_script_refreshes_ibus_registry(self) -> None:
        install_script = Path("install.sh").read_text(encoding="utf-8")

        self.assertIn("require_command ibus", install_script)
        self.assertIn('KDE_VIRTUAL_KEYBOARD_DESKTOP="/usr/share/applications/org.freedesktop.IBus.Panel.Wayland.Gtk3.desktop"', install_script)
        self.assertIn("kwriteconfig6", install_script)
        self.assertIn('--file "$HOME/.config/kwinrc"', install_script)
        self.assertIn("--group Wayland", install_script)
        self.assertIn("--key InputMethod", install_script)
        self.assertIn("--key VirtualKeyboardEnabled", install_script)
        self.assertIn("kdictate-plasma-wayland.sh", install_script)
        self.assertIn(".config/plasma-workspace/env", install_script)
        self.assertIn("kbuildsycoca6 --noincremental", install_script)
        self.assertIn("ibus write-cache", install_script)
        self.assertIn("ibus-daemon -drx -r -t refresh", install_script)
        self.assertIn("io.github.pizzimenti.KDictateToggle.desktop", install_script)
