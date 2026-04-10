#!/usr/bin/env python3
"""Regression check for forbidden injector backends in active paths."""

from __future__ import annotations

import sys
from pathlib import Path

FORBIDDEN_TOKENS = ("ydotool", "dotool", "wtype", "wl-copy", "xdotool", "type_text")
ACTIVE_PATHS = (
    Path("README.md"),
    Path("install.py"),
    Path("install.py"),
    Path("kdictate"),
    Path("packaging"),
)
REQUIRED_FILES = (
    Path("packaging/kdictate-plasma-wayland.sh"),
    Path("packaging/kdictate-systemd.service"),
    Path("packaging/io.github.pizzimenti.KDictate.service"),
    Path("packaging/io.github.pizzimenti.KDictate.component.xml"),
)


def iter_text_files(root: Path) -> list[Path]:
    """Return all text-ish files under the active path."""

    if root.is_file():
        return [root]
    return sorted(path for path in root.rglob("*") if path.is_file())


def main() -> int:
    """Run the forbidden-backend regression check."""

    print("==> Checking active paths for forbidden injector or clipboard backends")
    violations: list[str] = []
    for root in ACTIVE_PATHS:
        for path in iter_text_files(root):
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for token in FORBIDDEN_TOKENS:
                if token in text:
                    violations.append(f"{path}: {token}")

    if violations:
        print("Forbidden backend reference found in active paths.", file=sys.stderr)
        for violation in violations:
            print(violation, file=sys.stderr)
        return 1

    print("==> Checking required packaging assets")
    missing = [path for path in REQUIRED_FILES if not path.is_file()]
    if missing:
        for path in missing:
            print(f"Missing required file: {path}", file=sys.stderr)
        return 1

    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
