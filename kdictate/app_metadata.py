"""Canonical application metadata shared across runtime and packaging."""

from __future__ import annotations

from pathlib import Path
from typing import Final

APP_VERSION: Final[str] = "0.5.0"
DISPLAY_NAME: Final[str] = "KDictate"
APP_AUTHOR: Final[str] = "Bradley Pizzimenti"
APP_HOMEPAGE: Final[str] = "https://github.com/pizzimenti/kdictate"
APP_LICENSE: Final[str] = "MIT"
TEXTDOMAIN: Final[str] = "kdictate"

ENGINE_DESCRIPTION: Final[str] = "Session D-Bus driven dictation engine"
ENGINE_LANGUAGE: Final[str] = "en"
ENGINE_ICON: Final[str] = "audio-input-microphone"
ENGINE_LAYOUT: Final[str] = "default"
ENGINE_RANK: Final[str] = "1"  # str because it's serialised into the XML template

DEFAULT_MODEL_HF_REPO: Final[str] = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
DEFAULT_MODEL_NAME: Final[str] = "whisper-large-v3-turbo-ct2"
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
DEFAULT_MODEL_DIR: Final[Path] = PROJECT_ROOT / DEFAULT_MODEL_NAME
