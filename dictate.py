"""Compatibility entrypoint for the whisper-dictate daemon."""

from __future__ import annotations

from whisper_dictate.core.daemon import DictationDaemon, main

__all__ = ["DictationDaemon", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
