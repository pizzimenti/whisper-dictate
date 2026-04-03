from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from whisper_dictate.runtime import (
    RuntimePaths,
    default_runtime_paths,
    read_last_text,
    read_state,
    write_last_text,
    write_state,
)


class RuntimeHelpersTest(unittest.TestCase):
    def test_state_and_text_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = RuntimePaths(
                state_file=Path(tmpdir) / "daemon.state",
                last_text_file=Path(tmpdir) / "daemon.last.txt",
            )

            self.assertEqual(read_state(paths.state_file), "missing")
            self.assertEqual(read_last_text(paths.last_text_file), "")

            write_state(paths.state_file, "recording")
            write_last_text(paths.last_text_file, "hello world")

            self.assertEqual(read_state(paths.state_file), "recording")
            self.assertEqual(read_last_text(paths.last_text_file), "hello world")

    def test_default_runtime_paths_respects_xdg_runtime_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": tmpdir}, clear=False):
                paths = default_runtime_paths(uid=1234)
        self.assertEqual(paths.state_file, Path(tmpdir) / "whisper-dictate-1234.state")
        self.assertEqual(paths.last_text_file, Path(tmpdir) / "whisper-dictate-1234.last.txt")
