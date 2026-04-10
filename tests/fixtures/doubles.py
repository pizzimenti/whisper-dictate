"""Deterministic test doubles for the D-Bus and IBus contract surface."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from kdictate.constants import STATE_IDLE, STATE_RECORDING, STATE_TRANSCRIBING


@dataclass(frozen=True)
class SignalEvent:
    """Record a synthetic signal emitted by the fake D-Bus service."""

    name: str
    args: tuple[str, ...]


class FakeSignalBus:
    """Minimal in-memory signal bus for transcript events."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable[..., None]]] = {}
        self.emitted: list[SignalEvent] = []

    def subscribe(self, signal_name: str, callback: Callable[..., None]) -> None:
        """Register a callback for a specific signal name."""

        bucket = self._subscribers.setdefault(signal_name, [])
        if callback not in bucket:
            bucket.append(callback)

    def unsubscribe(self, signal_name: str, callback: Callable[..., None]) -> None:
        """Remove a previously registered callback."""

        bucket = self._subscribers.get(signal_name, [])
        if callback in bucket:
            bucket.remove(callback)

    def emit(self, signal_name: str, *args: str) -> None:
        """Emit a signal and synchronously fan it out to subscribers."""

        event = SignalEvent(signal_name, tuple(args))
        self.emitted.append(event)
        for callback in list(self._subscribers.get(signal_name, [])):
            callback(*args)


@dataclass
class FakeIbusContext:
    """Capture the preedit/commit operations an IBus engine would perform."""

    focused: bool = True
    preedit_text: str = ""
    committed_text: list[str] = field(default_factory=list)
    event_log: list[str] = field(default_factory=list)

    def focus_in(self) -> None:
        """Mark the context as focusable."""

        self.focused = True
        self.event_log.append("focus_in")

    def focus_out(self) -> None:
        """Mark the context as unfocused and clear transient state."""

        self.focused = False
        self.event_log.append("focus_out")
        self.clear_preedit()

    def set_preedit(self, text: str) -> None:
        """Store the current preedit text if the context is focused."""

        if not self.focused:
            self.event_log.append(f"ignored_preedit:{text}")
            return
        self.preedit_text = text
        self.event_log.append(f"preedit:{text}")

    def clear_preedit(self) -> None:
        """Clear the visible preedit string."""

        self.preedit_text = ""
        self.event_log.append("preedit_clear")

    def commit_text(self, text: str) -> None:
        """Record a committed transcript if the context is focused."""

        if not self.focused:
            self.event_log.append(f"ignored_commit:{text}")
            return
        self.committed_text.append(text)
        self.event_log.append(f"commit:{text}")


class FakeDbusService:
    """Model the daemon D-Bus API without any process or socket dependencies."""

    def __init__(self) -> None:
        self.bus = FakeSignalBus()
        self.state = STATE_IDLE
        self.last_text = ""
        self.last_error_code = ""
        self.last_error_message = ""
        self.active_partial = ""
        self.calls: list[str] = []

    def start(self) -> None:
        """Transition to recording and publish the new state."""

        self.calls.append("Start")
        self.state = STATE_RECORDING
        self.bus.emit("StateChanged", self.state)

    def stop(self) -> None:
        """Transition through transcribing and settle to idle.

        Mirrors the production daemon, which always ends a stop pathway
        with _write_state(STATE_IDLE) after _finalize_text. Without the
        IDLE follow-up, integration tests had to manually emit a final
        StateChanged("idle") to complete the lifecycle, masking any
        regression where the production daemon failed to reach IDLE.
        """

        self.calls.append("Stop")
        self.state = STATE_TRANSCRIBING
        self.bus.emit("StateChanged", self.state)
        self.state = STATE_IDLE
        self.bus.emit("StateChanged", self.state)

    def toggle(self) -> None:
        """Toggle between idle and recording states."""

        self.calls.append("Toggle")
        if self.state == STATE_IDLE:
            self.start()
            return
        self.stop()

    def get_state(self) -> str:
        """Return the current service state."""

        self.calls.append("GetState")
        return self.state

    def get_last_text(self) -> str:
        """Return the most recent completed transcript."""

        self.calls.append("GetLastText")
        return self.last_text

    def get_snapshot(self) -> tuple[str, str, str, str, str]:
        """Return a coarse session snapshot."""

        self.calls.append("GetSnapshot")
        return (self.state, self.active_partial, self.last_text,
                self.last_error_code, self.last_error_message)

    def ping(self) -> str:
        """Return the expected health-check response."""

        self.calls.append("Ping")
        return "pong"

    def emit_partial(self, text: str) -> None:
        """Publish a partial transcript event."""

        self.bus.emit("PartialTranscript", text)

    def emit_final(self, text: str) -> None:
        """Publish a final transcript event and update cached state."""

        self.last_text = text
        self.bus.emit("FinalTranscript", text)
        self.state = STATE_IDLE
        self.bus.emit("StateChanged", self.state)

    def emit_error(self, code: str, message: str) -> None:
        """Publish an error signal."""

        self.bus.emit("ErrorOccurred", code, message)


class TranscriptBridge:
    """Bridge fake D-Bus transcript events to a fake IBus focus context."""

    def __init__(
        self,
        bus: FakeSignalBus | None,
        context: FakeIbusContext,
        logger: logging.Logger | None = None,
    ) -> None:
        self._bus = bus
        self._context = context
        self._logger = logger or logging.getLogger("kdictate.tests.bridge")
        self.connected = False

    def attach(self, bus: FakeSignalBus | None) -> bool:
        """Attach to a bus and subscribe to transcript signals."""

        old_bus = self._bus
        self._bus = bus
        if bus is None:
            self._logger.warning("daemon unavailable")
            self.connected = False
            return False

        if old_bus is not None:
            old_bus.unsubscribe("PartialTranscript", self._on_partial_transcript)
            old_bus.unsubscribe("FinalTranscript", self._on_final_transcript)
            old_bus.unsubscribe("StateChanged", self._on_state_changed)
            old_bus.unsubscribe("ErrorOccurred", self._on_error)
        bus.subscribe("PartialTranscript", self._on_partial_transcript)
        bus.subscribe("FinalTranscript", self._on_final_transcript)
        bus.subscribe("StateChanged", self._on_state_changed)
        bus.subscribe("ErrorOccurred", self._on_error)
        self.connected = True
        return True

    def _on_partial_transcript(self, text: str) -> None:
        self._logger.info("partial transcript received: %s", text)
        self._context.set_preedit(text)

    def _on_final_transcript(self, text: str) -> None:
        self._logger.info("final transcript received: %s", text)
        self._context.clear_preedit()
        self._context.commit_text(text)

    def _on_state_changed(self, state: str) -> None:
        self._logger.info("state changed: %s", state)
        if state == STATE_IDLE:
            self._context.clear_preedit()

    def _on_error(self, code: str, message: str) -> None:
        self._logger.error("service error %s: %s", code, message)
