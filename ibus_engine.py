#!/usr/bin/env python3
"""Compatibility entrypoint for the whisper-dictate IBus engine."""

from __future__ import annotations

from whisper_dictate.ibus_engine.main import main


if __name__ == "__main__":
    raise SystemExit(main())
