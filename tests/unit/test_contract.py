"""Contract tests for the frozen D-Bus and package naming surface."""

from __future__ import annotations

import xml.etree.ElementTree as ET
import unittest

from whisper_dictate.constants import (
    APP_ROOT_ID,
    CANONICAL_STATES,
    DBUS_BUS_NAME,
    DBUS_INTERFACE,
    DBUS_OBJECT_PATH,
    STATE_ERROR,
    STATE_IDLE,
    STATE_RECORDING,
    STATE_STARTING,
    STATE_TRANSCRIBING,
)
from whisper_dictate.service.dbus_api import DBUS_INTROSPECTION_XML


class ContractTests(unittest.TestCase):
    """Verify the canonical service contract stays stable."""

    def test_names_and_states_are_stable(self) -> None:
        self.assertEqual(APP_ROOT_ID, "io.github.pizzimenti.WhisperDictate")
        self.assertEqual(DBUS_BUS_NAME, "io.github.pizzimenti.WhisperDictate1")
        self.assertEqual(DBUS_OBJECT_PATH, "/io/github/pizzimenti/WhisperDictate1")
        self.assertEqual(DBUS_INTERFACE, "io.github.pizzimenti.WhisperDictate1")
        self.assertEqual(CANONICAL_STATES, (STATE_IDLE, STATE_STARTING, STATE_RECORDING, STATE_TRANSCRIBING, STATE_ERROR))

    def test_dbus_introspection_contains_required_methods_and_signals(self) -> None:
        root = ET.fromstring(DBUS_INTROSPECTION_XML[DBUS_INTROSPECTION_XML.index("<node>"):])  # noqa: S314
        interface = root.find("interface")
        self.assertIsNotNone(interface)
        assert interface is not None
        self.assertEqual(interface.attrib["name"], DBUS_INTERFACE)

        methods = [node.attrib["name"] for node in interface.findall("method")]
        signals = [node.attrib["name"] for node in interface.findall("signal")]

        self.assertEqual(methods, ["Start", "Stop", "Toggle", "GetState", "GetLastText", "Ping"])
        self.assertEqual(signals, ["StateChanged", "PartialTranscript", "FinalTranscript", "ErrorOccurred"])

        get_state = interface.find("method[@name='GetState']")
        get_last_text = interface.find("method[@name='GetLastText']")
        ping = interface.find("method[@name='Ping']")
        self.assertIsNotNone(get_state)
        self.assertIsNotNone(get_last_text)
        self.assertIsNotNone(ping)

        for node, arg_name in ((get_state, "state"), (get_last_text, "text"), (ping, "response")):
            assert node is not None
            out_arg = node.find("arg")
            self.assertIsNotNone(out_arg)
            assert out_arg is not None
            self.assertEqual(out_arg.attrib["direction"], "out")
            self.assertEqual(out_arg.attrib["name"], arg_name)
            self.assertEqual(out_arg.attrib["type"], "s")
