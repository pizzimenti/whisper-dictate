"""Core IBus text-placement state machine for kdictate."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Literal, Protocol

from kdictate.constants import (
    STATE_ERROR,
    STATE_IDLE,
    STATE_RECORDING,
    STATE_STARTING,
    STATE_TRANSCRIBING,
)

AnimationMode = Literal["listening", "transcribing"]


@dataclass(frozen=True, slots=True)
class PreeditPresentation:
    """Rendered preedit intent produced by the controller."""

    partial: str
    mode: AnimationMode


class EngineAdapter(Protocol):
    """Minimal text-placement surface used by the controller.

    The concrete runtime adapter translates these operations into IBus API
    calls. Tests use a fake adapter to assert the exact render sequence.
    """

    def set_preedit(self, presentation: PreeditPresentation | None) -> None:
        """Render a preedit presentation, or hide preedit when ``None``."""

    def commit_text(self, text: str) -> None:
        """Commit finalized text to the focused application."""


@dataclass(slots=True)
class EngineState:
    """Mutable runtime state for the IBus frontend."""

    enabled: bool = False
    focused: bool = False
    daemon_available: bool = False
    daemon_state: str = STATE_IDLE
    pending_partial: str = ""
    last_final: str = ""
    last_error_code: str = ""
    last_error_message: str = ""
    preedit_visible: bool = False
    deferred_text: str = ""


class DictationEngineController:
    """Pure controller that decides when to show preedit or commit text.

    The controller keeps the IBus-facing policy separate from the transport and
    from the actual `IBus.Engine` subclass. This makes the focus and reconnect
    behavior testable without the IBus typelib.
    """

    def __init__(self, adapter: EngineAdapter, logger: logging.Logger) -> None:
        self._adapter = adapter
        self._logger = logger
        self._state = EngineState()

    @property
    def state(self) -> EngineState:
        """Return a copy of the current runtime state."""

        return replace(self._state)

    def enable(self) -> None:
        """Mark the engine enabled and restore any pending preedit."""

        if self._state.enabled:
            self._logger.info("IBus engine enable requested while already enabled")
            return

        self._state.enabled = True
        self._logger.info("IBus engine enabled")
        self._sync_preedit(reason="enable")

    def disable(self) -> None:
        """Mark the engine disabled and clear any stale preedit."""

        if not self._state.enabled:
            self._logger.info("IBus engine disable requested while already disabled")
        self._state.enabled = False
        self._state.focused = False
        self._hide_preedit(reason="disable")
        self._logger.info("IBus engine disabled")

    def focus_in(self) -> None:
        """Record focus arrival, flush deferred text if still dictating, and
        restore the current partial transcript."""

        self._state.focused = True
        self._logger.debug("IBus engine focus in")

        if self._state.deferred_text:
            if self._state.daemon_state in {STATE_RECORDING, STATE_TRANSCRIBING}:
                self._logger.info(
                    "Flushing deferred text on focus return (%d chars)",
                    len(self._state.deferred_text),
                )
                self._adapter.commit_text(self._state.deferred_text)
            else:
                self._logger.info(
                    "Discarding deferred text (dictation ended while unfocused, %d chars)",
                    len(self._state.deferred_text),
                )
            self._state.deferred_text = ""

        self._sync_preedit(reason="focus-in")

    def focus_out(self) -> None:
        """Record focus loss and clear visible preedit deterministically."""

        self._state.focused = False
        self._logger.debug("IBus engine focus out")
        self._hide_preedit(reason="focus-out")

    def reset(self) -> None:
        """Clear the visible preedit without changing focus ownership."""

        self._logger.debug("IBus engine reset")
        self._hide_preedit(reason="reset")

    def set_daemon_available(self, available: bool) -> None:
        """Update daemon reachability and reconcile any visible state."""

        if self._state.daemon_available == available:
            return

        self._state.daemon_available = available
        if available:
            self._logger.info("IBus engine connected to kdictate daemon")
            self._sync_preedit(reason="daemon-available")
        else:
            self._logger.warning("IBus engine lost kdictate daemon connection")
            self._state.pending_partial = ""
            self._hide_preedit(reason="daemon-lost")

    def handle_state_changed(self, state: str) -> None:
        """Apply a daemon state transition."""

        if state not in {STATE_IDLE, STATE_STARTING, STATE_RECORDING, STATE_TRANSCRIBING, STATE_ERROR}:
            self._logger.warning("Ignoring unknown daemon state %r", state)
            self._state.daemon_state = STATE_ERROR
            self._state.pending_partial = ""
            self._hide_preedit(reason="invalid-state")
            return

        previous = self._state.daemon_state
        self._state.daemon_state = state
        self._logger.info("Daemon state changed: %s -> %s", previous, state)

        if state in {STATE_IDLE, STATE_STARTING, STATE_ERROR}:
            self._state.pending_partial = ""
            if self._state.deferred_text:
                self._logger.info(
                    "Discarding deferred text on state %s (%d chars)",
                    state, len(self._state.deferred_text),
                )
                self._state.deferred_text = ""
            self._hide_preedit(reason=f"state-{state}")
            return

        if state in {STATE_RECORDING, STATE_TRANSCRIBING}:
            if self._can_render_status():
                self._show_preedit(self._state.pending_partial, self._live_mode())
                if state == STATE_TRANSCRIBING:
                    self._logger.info("Transcribing animation started")
            else:
                self._hide_preedit(reason=f"{state}-without-focus")
            return

    def handle_partial_transcript(self, text: str) -> None:
        """Render partial transcript text into the preedit buffer."""

        normalized = self._normalize_text(text)
        self._state.pending_partial = normalized

        if not self._can_render_live_text():
            if normalized:
                self._logger.info(
                    "Caching partial transcript until focus is available: daemon=%s focused=%s enabled=%s state=%s",
                    self._state.daemon_available,
                    self._state.focused,
                    self._state.enabled,
                    self._state.daemon_state,
                )
            return

        self._logger.debug("Updating preedit from partial transcript (%d chars)", len(normalized))
        self._show_preedit(normalized, self._live_mode())

    def handle_final_transcript(self, text: str) -> None:
        """Commit a finalized transcript through IBus only."""

        normalized = self._normalize_text(text)
        self._state.last_final = normalized
        self._state.pending_partial = ""

        if not normalized:
            self._logger.debug("Ignoring empty final transcript")
            self._hide_preedit(reason="empty-final")
            return

        if not self._can_commit_final():
            self._state.deferred_text += (" " + normalized if self._state.deferred_text else normalized)
            self._logger.info(
                "Deferred final transcript (no focus): %d chars, total deferred %d chars",
                len(normalized), len(self._state.deferred_text),
            )
            self._hide_preedit(reason="final-deferred")
            return

        self._logger.info("Committing final transcript through IBus (%d chars)", len(normalized))
        self._hide_preedit(reason="final-before-commit")
        self._adapter.commit_text(normalized)

    def handle_error(self, code: str, message: str) -> None:
        """Record a recoverable error from the daemon."""

        self._state.last_error_code = str(code)
        self._state.last_error_message = message
        self._state.daemon_state = STATE_ERROR
        self._state.pending_partial = ""
        self._logger.error("Daemon error occurred: code=%s message=%s", code, message)
        self._hide_preedit(reason="daemon-error")

    def _can_render_status(self) -> bool:
        """Whether it is safe to push any preedit content (animations included)."""
        return (
            self._state.enabled
            and self._state.focused
            and self._state.daemon_available
        )

    def _can_render_live_text(self) -> bool:
        return (
            self._state.enabled
            and self._state.focused
            and self._state.daemon_available
            and self._state.daemon_state in {STATE_RECORDING, STATE_TRANSCRIBING}
        )

    def _live_mode(self) -> AnimationMode:
        if self._state.daemon_state == STATE_TRANSCRIBING:
            return "transcribing"
        return "listening"

    def _can_commit_final(self) -> bool:
        # Intentionally does NOT check daemon_state.  FinalTranscript and
        # StateChanged(idle) are both queued via GLib.idle_add on the daemon
        # side, so the commit handler may be called before the state update
        # lands in the controller.  Gating on daemon_state would silently drop
        # text whenever idle arrives first.
        return self._state.enabled and self._state.focused and self._state.daemon_available

    def _sync_preedit(self, *, reason: str) -> None:
        if self._state.pending_partial and self._can_render_live_text():
            self._logger.debug("Restoring preedit after %s", reason)
            self._show_preedit(self._state.pending_partial, self._live_mode())
            return

        if self._state.pending_partial:
            self._logger.debug(
                "Pending partial not rendered after %s because the engine is not ready",
                reason,
            )

    def _show_preedit(self, partial: str, mode: AnimationMode) -> None:
        self._state.preedit_visible = True
        self._adapter.set_preedit(PreeditPresentation(partial=partial, mode=mode))

    def _hide_preedit(self, *, reason: str) -> None:
        if self._state.preedit_visible:
            self._logger.debug("Hiding preedit due to %s", reason)
        self._state.preedit_visible = False
        self._adapter.set_preedit(None)

    @staticmethod
    def _normalize_text(text: str) -> str:
        """Normalize transcript text for safe IBus rendering."""

        return " ".join(text.replace("\r", " ").replace("\n", " ").split())
