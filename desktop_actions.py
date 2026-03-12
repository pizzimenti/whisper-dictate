"""Desktop-side helpers shared by the daemon and the hotkey listener."""

from __future__ import annotations

import subprocess


DEFAULT_APP_NAME = "whisper-dictate"
DEFAULT_NOTIFY_TIMEOUT_MS = 3000


def notify(message: str, *, app_name: str = DEFAULT_APP_NAME, timeout_ms: int = DEFAULT_NOTIFY_TIMEOUT_MS) -> None:
    """Show a best-effort desktop notification without blocking the caller."""

    subprocess.Popen(
        ["notify-send", "-a", app_name, "-t", str(timeout_ms), message],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def type_text(text: str) -> subprocess.CompletedProcess[bytes]:
    """Type text into the current keyboard focus via ydotool."""

    return subprocess.run(["ydotool", "type", "--", text], check=False)
