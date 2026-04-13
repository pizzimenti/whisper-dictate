"""IBus preedit render adapter with a simple spinner animation.

The adapter owns a small animation state machine driven by a GLib timer.
At 5 Hz it cycles through ``_SPINNER_FRAMES`` and renders the current
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

_SPINNER_MS = 200  # 4 frames × 200 ms = 800 ms per full rotation (5 Hz frame rate)
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
        # Track whether an IBus preedit is currently on-screen so
        # _clear_preedit can skip the IBus hide round-trip when nothing
        # is visible and so shutdown() knows whether it needs to call
        # the hide API or can just drop state.
        self._visible: bool = False

    def set_preedit(self, presentation: PreeditPresentation | None) -> None:
        """Render a preedit presentation, or hide preedit when ``None``."""
        if presentation is None:
            self._clear_preedit()
            return
        if self._mode != presentation.mode:
            self._frame = 0
        self._partial = presentation.partial
        self._mode = presentation.mode
        self._render()
        if self._timer_id is None:
            self._timer_id = GLib.timeout_add(_SPINNER_MS, self._tick)

    def commit_text(self, text: str) -> None:
        """Commit finalized text. Preedit cleanup is owned by the controller."""
        self._engine.commit_text(self._ibus.Text.new_from_string(text))

    def shutdown(self) -> None:
        """Tear down the adapter: stop the spinner timer and make sure
        no stale preedit is left on-screen.

        Safe to call repeatedly, and safe to call from IBus tear-down
        paths where the engine may be part-way through destruction —
        the IBus hide round-trip is wrapped so any raise from the
        bound methods is swallowed (the adapter state is still reset
        regardless).
        """
        try:
            self._clear_preedit()
        except Exception:  # noqa: BLE001
            # IBus may already be tearing the engine down.
            self._stop_timer()
            self._visible = False
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
        if not self._visible:
            # Nothing on-screen — skip the round-trip to IBus. Prevents
            # spurious hide_preedit_text calls on focus-out while the
            # preedit was never shown in the first place.
            return
        # Use update_preedit_text_with_mode with visible=False to hide the
        # preedit.  Do NOT send a separate hide_preedit_text() signal —
        # Chromium's IBus client interprets that as "commit the current
        # preedit buffer", which causes the status-animation text to be
        # inserted into the focused field instead of the final transcript.
        self._engine.update_preedit_text_with_mode(
            self._ibus.Text.new_from_string(""), 0, False,
            self._ibus.PreeditFocusMode.CLEAR,
        )
        self._visible = False

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
        self._visible = True
