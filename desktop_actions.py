"""Desktop-side helpers shared by the daemon and the hotkey listener."""

from __future__ import annotations

import subprocess


DEFAULT_APP_NAME = "whisper-dictate"
DEFAULT_NOTIFY_TIMEOUT_MS = 3000

def _gdbus_notify(message: str, replace_id: int = 0, timeout_ms: int = DEFAULT_NOTIFY_TIMEOUT_MS, app_name: str = DEFAULT_APP_NAME) -> int:
    """Send a notification via gdbus. Returns the notification ID (0 on failure)."""
    cmd = [
        "gdbus", "call", "--session",
        "--dest", "org.freedesktop.Notifications",
        "--object-path", "/org/freedesktop/Notifications",
        "--method", "org.freedesktop.Notifications.Notify",
        app_name,           # app_name
        str(replace_id),    # replaces_id (0 = new)
        "",                 # app_icon
        app_name,           # summary
        message,            # body
        "[]",               # actions
        "{}",               # hints
        str(timeout_ms),    # expire_timeout
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if result.returncode == 0 and result.stdout.strip():
            out = result.stdout.strip()
            start = out.find("uint32 ")
            if start != -1:
                end = out.find(",", start)
                id_str = out[start + 7 : end if end != -1 else None].strip()
                if id_str.isdigit():
                    return int(id_str)
    except Exception:  # noqa: BLE001
        pass
    return 0


def notify(message: str, *, timeout_ms: int = DEFAULT_NOTIFY_TIMEOUT_MS) -> None:
    """Show a one-shot notification (no replace). Use for startup/errors."""
    _gdbus_notify(message, replace_id=0, timeout_ms=timeout_ms)


class DictationNotifier:
    """Manage the notification lifecycle for a single recording session.

    Create → replace → replace keeps the notification visible and in-place.
    """

    def __init__(self) -> None:
        self._session_id: int = 0

    def started(self, mic_name: str = "") -> None:
        """New notification for recording start (persistent until replaced)."""
        msg = f"🎙️ listening on\n{mic_name}" if mic_name else "🎙️ listening..."
        self._session_id = _gdbus_notify(msg, replace_id=0, timeout_ms=0)

    def transcribing(self) -> None:
        """Replace listening with transcribing status (persistent until complete)."""
        if self._session_id:
            self._session_id = _gdbus_notify("dictation stopped. 💬 transcribing...", replace_id=self._session_id, timeout_ms=0)

    def stopped(self) -> None:
        """Replace with completion notice, then let it expire."""
        if self._session_id:
            _gdbus_notify("transcription complete", replace_id=self._session_id)
        self._session_id = 0


def type_text(text: str) -> None:
    """Copy text to the Wayland clipboard for manual paste."""
    subprocess.run(["wl-copy", "--", text], check=True, timeout=3)
