"""Map HUD model to presentation values -- no UI dependencies."""

from __future__ import annotations

from dataclasses import dataclass

from kdictate.hud.state import HudModel, HudPhase

MAX_DISPLAY_LENGTH = 80


@dataclass(frozen=True, slots=True)
class HudPresentation:
    """What the HUD window should render right now."""

    visible: bool
    label: str
    style: str  # "neutral", "active", "success", "error"


def present(model: HudModel) -> HudPresentation:
    """Derive the current visual presentation from the HUD model."""

    if model.phase == HudPhase.HIDDEN:
        return HudPresentation(visible=False, label="", style="neutral")

    if model.phase == HudPhase.STARTING:
        return HudPresentation(visible=True, label="Starting\u2026", style="neutral")

    if model.phase == HudPhase.LISTENING:
        return HudPresentation(visible=True, label="Listening\u2026", style="active")

    if model.phase == HudPhase.PARTIAL:
        return HudPresentation(
            visible=True,
            label=_truncate(model.partial_text),
            style="active",
        )

    if model.phase == HudPhase.TRANSCRIBING:
        return HudPresentation(visible=True, label="Transcribing\u2026", style="neutral")

    if model.phase == HudPhase.COMMITTING:
        return HudPresentation(
            visible=True,
            label=f"Committed: {_truncate(model.commit_text)}",
            style="success",
        )

    if model.phase == HudPhase.ERROR:
        return HudPresentation(
            visible=True,
            label=model.error_message or "Unknown error",
            style="error",
        )

    return HudPresentation(visible=False, label="", style="neutral")


def _truncate(text: str, max_len: int = MAX_DISPLAY_LENGTH) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."
