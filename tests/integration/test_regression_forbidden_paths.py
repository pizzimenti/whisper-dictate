"""Regression checks for forbidden injector backends in active paths."""

from __future__ import annotations

from pathlib import Path
import unittest


FORBIDDEN_TOKENS = ("ydotool", "dotool", "wtype", "wl-copy", "xdotool")
ALLOWED_SUFFIXES = {".py", ".sh", ".xml", ".service", ".desktop", ".md"}
ACTIVE_PATHS = (
    Path("install.py"),
    Path("kdictate"),
    Path("packaging"),
)


class ForbiddenPathRegressionTests(unittest.TestCase):
    """Protect the active IBus-focused code paths from injector regressions."""

    def test_forbidden_backends_are_absent_from_active_paths(self) -> None:
        violations: list[str] = []
        for root in ACTIVE_PATHS:
            paths = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.is_file())
            for path in paths:
                if path.suffix not in ALLOWED_SUFFIXES:
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                for token in FORBIDDEN_TOKENS:
                    if token in text:
                        violations.append(f"{path}: {token}")

        self.assertEqual(violations, [], msg="Forbidden backend references found:\n" + "\n".join(violations))
