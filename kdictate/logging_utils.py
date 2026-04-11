"""Shared logging helpers for long-lived kdictate processes."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TextIO

DEFAULT_LOG_FORMAT = (
    "%(asctime)s %(levelname)s %(name)s "
    "[pid=%(process)d thread=%(threadName)s] %(message)s"
)
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _formatter() -> logging.Formatter:
    return logging.Formatter(DEFAULT_LOG_FORMAT, DEFAULT_DATE_FORMAT)


def _resolve_log_dir() -> Path | None:
    """Return the XDG state directory for kdictate logs, or None."""

    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        log_dir = Path(state_home) / "kdictate"
    else:
        log_dir = Path.home() / ".local" / "state" / "kdictate"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir
    except OSError:
        return None


def configure_logging(
    logger_name: str,
    *,
    level: int = logging.INFO,
    stream: TextIO | None = None,
    log_file: str | None = None,
) -> logging.Logger:
    """Configure and return a named logger with a stderr (and optional file) sink.

    When *log_file* is given (just a filename, not a path), a file handler
    is also attached under ``$XDG_STATE_HOME/kdictate/<log_file>`` so
    subprocesses whose stderr is /dev/null still produce debuggable output.
    """

    logger = logging.getLogger(logger_name)
    logger.setLevel(level)

    if not _has_stream_handler(logger):
        stderr_handler = logging.StreamHandler(stream)
        stderr_handler.setFormatter(_formatter())
        logger.addHandler(stderr_handler)

    if log_file:
        attach_file_handler(logger, log_file)

    logger.propagate = False
    return logger


def attach_file_handler(logger: logging.Logger, filename: str) -> None:
    """Attach a file handler under XDG_STATE_HOME/kdictate/<filename>.

    Idempotent: if a file handler for the same path is already present,
    it is left alone. Silently no-ops if the directory cannot be created.
    """

    log_dir = _resolve_log_dir()
    if log_dir is None:
        return
    target = log_dir / filename
    target_str = str(target)
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler) and handler.baseFilename == target_str:
            return
    try:
        file_handler = logging.FileHandler(target, mode="a", encoding="utf-8")
    except OSError:
        return
    file_handler.setFormatter(_formatter())
    logger.addHandler(file_handler)


def _has_stream_handler(logger: logging.Logger) -> bool:
    return any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in logger.handlers
    )
