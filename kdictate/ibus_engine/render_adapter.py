"""IBus preedit render adapter with a simple spinner animation.

The adapter owns a small animation state machine driven by a GLib timer.
At 5Hz it cycles through ``_SPINNER_FRAMES`` and renders the current
frame as the trailing spinner next to the listening / transcribing label.

Kept in its own module so the engine wiring stays small and the adapter
can be unit-tested as a public class instead of a private implementation
detail of ``engine.py``.
"""

from __future__ import annotations

from types import ModuleType
from typing import Any

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib

from kdictate.ibus_engine.controller import PreeditPresentation

_SPINNER_MS = 125  # 4 frames × 125ms = 500ms per full rotation
_SPINNER_FRAMES = ("\u25d0", "\u25d3", "\u25d1", "\u25d2")  # ◐ ◓ ◑ ◒
_MIC = "\U0001f399"  # 🎙 (studio microphone)
_BRAIN = "\U0001f9e0"  # 🧠
_UNICORN = "\U0001f984"  # 🦄
_LISTENING_LABEL = f"{_MIC} Listening..."
_TRANSCRIBING_LABEL = f"{_BRAIN} Transcribing..."


class IbusRenderAdapter:
    """Translate controller render operations into IBus API calls."""

    def __init__(self, engine: Any, ibus_module: ModuleType) -> None:
        self._engine = engine
        self._ibus = ibus_module
        self._timer_id: int | None = None
        self._frame: int = 0
        self._mode: str = "idle"  # "idle" | "listening" | "transcribing"
        self._partial: str = ""

    def set_preedit(self, presentation: PreeditPresentation | None) -> None:
        """Render a preedit presentation, or hide preedit when ``None``."""
        if presentation is None:
            self._clear_preedit()
            return
        self._partial = presentation.partial
        self._mode = presentation.mode
        self._render()
        if self._timer_id is None:
            self._timer_id = GLib.timeout_add(_SPINNER_MS, self._tick)

    def commit_text(self, text: str) -> None:
        """Commit finalized text. Preedit cleanup is owned by the controller."""
        self._engine.commit_text(self._ibus.Text.new_from_string(text))

    def shutdown(self) -> None:
        """Stop any running animation timer.  Safe to call repeatedly."""
        self._stop_timer()
        self._mode = "idle"
        self._partial = ""

    # -- Internal -----------------------------------------------------------

    def _stop_timer(self) -> None:
        if self._timer_id is not None:
            GLib.source_remove(self._timer_id)
            self._timer_id = None

    def _clear_preedit(self) -> None:
        self._stop_timer()
        self._mode = "idle"
        self._partial = ""
        self._engine.update_preedit_text_with_mode(
            self._ibus.Text.new_from_string(""), 0, False,
            self._ibus.PreeditFocusMode.CLEAR,
        )
        self._engine.hide_preedit_text()

    def _tick(self) -> bool:
        self._frame = (self._frame + 1) % len(_SPINNER_FRAMES)
        self._render()
        return GLib.SOURCE_CONTINUE

    def _compose_frame(self) -> str:
        spinner = _SPINNER_FRAMES[self._frame]
        if self._mode == "listening":
            if self._partial:
                return f"{self._partial} {_UNICORN} {spinner}"
            return f"{_LISTENING_LABEL} {spinner}"
        if self._mode == "transcribing":
            if self._partial:
                return f"{self._partial} {_UNICORN} {_TRANSCRIBING_LABEL} {spinner}"
            return f"{_TRANSCRIBING_LABEL} {spinner}"
        return ""

    def _render(self) -> None:
        frame = self._compose_frame()
        if not frame:
            return
        ibus_text = self._ibus.Text.new_from_string(frame)
        self._engine.update_preedit_text_with_mode(
            ibus_text, len(frame), True, self._ibus.PreeditFocusMode.CLEAR,
        )
        self._engine.show_preedit_text()
