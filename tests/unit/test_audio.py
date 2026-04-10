"""Tests for PulseAudio/PipeWire input-device probing helpers."""

from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from kdictate.core.audio import resolve_default_input_device


class AudioHelpersTest(unittest.TestCase):
    def test_resolve_default_input_device_uses_utf8_and_returns_description(self) -> None:
        side_effect = [
            subprocess.CompletedProcess(
                args=["pactl", "get-default-source"],
                returncode=0,
                stdout="alsa_input.pci-0000_00_1f.3.analog-stereo\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["pactl", "list", "sources"],
                returncode=0,
                stdout=(
                    "Name: alsa_input.pci-0000_00_1f.3.analog-stereo\n"
                    "Description: Built-in Audio Analog Stereo\n"
                ),
                stderr="",
            ),
        ]

        with mock.patch("kdictate.core.audio.subprocess.run", side_effect=side_effect) as run:
            self.assertEqual(
                resolve_default_input_device(),
                ("Built-in Audio Analog Stereo", True),
            )

        first_call = run.call_args_list[0]
        self.assertEqual(first_call.kwargs["encoding"], "utf-8")
        self.assertEqual(first_call.kwargs["errors"], "replace")

    def test_resolve_default_input_device_rejects_monitor_source(self) -> None:
        result = subprocess.CompletedProcess(
            args=["pactl", "get-default-source"],
            returncode=0,
            stdout="alsa_output.monitor\n",
            stderr="",
        )

        with mock.patch("kdictate.core.audio.subprocess.run", return_value=result):
            self.assertEqual(resolve_default_input_device(), ("alsa_output.monitor", False))

    def test_resolve_default_input_device_returns_unknown_when_pactl_fails(self) -> None:
        with mock.patch("kdictate.core.audio.subprocess.run", side_effect=OSError("missing pactl")):
            self.assertEqual(resolve_default_input_device(), ("unknown", False))
