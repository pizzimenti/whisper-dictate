"""HUD controller -- coordinates the state reducer, timer, and rendering surface.

This module has no GTK or GLib imports so the controller is unit-testable
without a display server or running main loop.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

from kdictate.hud.state import (
    CommitAckExpired,
    HudEvent,
    HudModel,
    HudPhase,
    reduce,
)
from kdictate.hud.view_model import HudPresentation, present

COMMIT_ACK_MS = 2000


class HudSurface(Protocol):
    """Minimal rendering surface used by the controller."""

    def update_presentation(self, presentation: HudPresentation) -> None: ...


class TimerScheduler(Protocol):
    """Schedule and cancel a one-shot timer callback."""

    def schedule(self, delay_ms: int, callback: Callable[[], None]) -> int: ...
    def cancel(self, timer_id: int) -> None: ...


class HudController:
    """Coordinate the state reducer, commit timer, and rendering surface.

    Both *window* and *timer* are injected so the dispatch path is
    testable without a live display or event loop.
    """

    def __init__(
        self,
        *,
        window: HudSurface,
        timer: TimerScheduler,
        logger: logging.Logger,
    ) -> None:
        self._logger = logger
        self._model = HudModel()
        self._commit_timer_id: int | None = None
        self._window = window
        self._timer = timer

    @property
    def model(self) -> HudModel:
        return self._model

    def dispatch(self, event: HudEvent) -> None:
        """Apply an event to the state reducer and update the surface."""

        old = self._model
        self._model = reduce(old, event)
        if self._model == old:
            return

        presentation = present(self._model)
        self._window.update_presentation(presentation)

        if self._model.phase == HudPhase.COMMITTING:
            self._start_commit_timer()
        elif old.phase == HudPhase.COMMITTING and self._model.phase != HudPhase.COMMITTING:
            self._cancel_commit_timer()

    def _start_commit_timer(self) -> None:
        self._cancel_commit_timer()
        self._commit_timer_id = self._timer.schedule(
            COMMIT_ACK_MS, self._on_commit_expired,
        )

    def _cancel_commit_timer(self) -> None:
        if self._commit_timer_id is not None:
            self._timer.cancel(self._commit_timer_id)
            self._commit_timer_id = None

    def _on_commit_expired(self) -> None:
        self._commit_timer_id = None
        self.dispatch(CommitAckExpired())
