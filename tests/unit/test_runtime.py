from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kdictate.runtime import (
    RuntimePaths,
    atomic_write_text,
    default_runtime_paths,
    read_last_text,
    read_state,
    wait_for_state,
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
        self.assertEqual(paths.state_file, Path(tmpdir) / "kdictate-1234.state")
        self.assertEqual(paths.last_text_file, Path(tmpdir) / "kdictate-1234.last.txt")

    def test_default_runtime_paths_error_mentions_uid(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("pathlib.Path.is_dir", return_value=False),
        ):
            with self.assertRaisesRegex(RuntimeError, "/run/user/1234"):
                default_runtime_paths(uid=1234)

    def test_write_state_is_atomic_and_leaves_no_tmp_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "daemon.state"
            write_state(state_file, "recording")
            self.assertEqual(state_file.read_text(encoding="utf-8"), "recording\n")
            self.assertFalse((Path(tmpdir) / "daemon.state.tmp").exists())

    def test_atomic_write_text_overwrites_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "last.txt"
            atomic_write_text(target, "hello")
            atomic_write_text(target, "world")
            self.assertEqual(target.read_text(encoding="utf-8"), "world")

    def test_wait_for_state_times_out_when_target_is_not_reached(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "daemon.state"
            write_state(state_file, "idle")
            self.assertIsNone(wait_for_state(state_file, {"recording"}, 0.01, poll_interval=0.001))
