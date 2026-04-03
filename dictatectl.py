#!/usr/bin/env python3
"""Compatibility entrypoint for the whisper-dictate control helper."""

from __future__ import annotations

from whisper_dictate.cli.dictatectl import main


if __name__ == "__main__":
    raise SystemExit(main())
