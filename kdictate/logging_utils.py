"""Shared logging helpers for long-lived kdictate processes."""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TextIO

DEFAULT_LOG_FORMAT = (
    "%(asctime)s.%(msecs)03d %(levelname)s %(name)s "
    "[%(threadName)s] %(message)s"
)
DEFAULT_DATE_FORMAT = "%H:%M:%S"

# Logs rotate at 1 MB, keep 1 backup (so max ~2 MB per log file).
_MAX_LOG_BYTES = 1_000_000
_BACKUP_COUNT = 1


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

    When *log_file* is given (just a filename, not a path), a rotating
    file handler is attached under ``$XDG_STATE_HOME/kdictate/<log_file>``
    so subprocesses whose stderr is /dev/null still produce debuggable
    output. Logs rotate at 1 MB with 1 backup (~2 MB max per file).
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


def get_propagating_child(parent: logging.Logger, suffix: str) -> logging.Logger:
    """Return a child logger that funnels its records up to *parent*.

    Use this when several long-lived subsystems share a single log file:
    attaching multiple ``FileHandler`` instances to the same path produces
    interleaved/garbled output because Python's logging module gives each
    handler its own lock with no cross-handler coordination. Instead,
    attach one ``FileHandler`` to the parent and let children propagate
    into it.

    The returned logger is reset on every call: any handlers carried
    over from prior initialization are removed and ``propagate`` is
    forced to ``True``. This makes the call safe even if some other
    import path has already touched ``logging.getLogger(parent.name +
    "." + suffix)`` and left it in an unexpected state.
    """

    child = parent.getChild(suffix)
    for handler in list(child.handlers):
        child.removeHandler(handler)
    child.propagate = True
    return child


def attach_file_handler(logger: logging.Logger, filename: str) -> None:
    """Attach a rotating file handler under XDG_STATE_HOME/kdictate/<filename>.

    Idempotent: if a file handler for the same path is already present,
    it is left alone. Silently no-ops if the directory cannot be created.
    Rotates at 1 MB, keeps 1 backup.
    """

    log_dir = _resolve_log_dir()
    if log_dir is None:
        return
    target = log_dir / filename
    target_str = str(target)
    for handler in logger.handlers:
        if isinstance(handler, (logging.FileHandler, RotatingFileHandler)):
            if handler.baseFilename == target_str:
                return
    try:
        file_handler = RotatingFileHandler(
            target, maxBytes=_MAX_LOG_BYTES, backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError:
        return
    file_handler.setFormatter(_formatter())
    logger.addHandler(file_handler)


def _has_stream_handler(logger: logging.Logger) -> bool:
    return any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in logger.handlers
    )
