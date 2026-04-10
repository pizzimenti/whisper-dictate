"""HUD companion process -- wires D-Bus bridge, controller, and GTK window."""

from __future__ import annotations

import logging
import signal
import sys
from collections.abc import Callable

from kdictate.hud.controller import HudController
from kdictate.hud.dbus_client import HudDaemonBridge
from kdictate.hud.state import (
    DaemonAppeared,
    DaemonStateChanged,
    DaemonVanished,
    ErrorOccurred,
    FinalTranscript,
    PartialTranscript,
    SnapshotReceived,
)
from kdictate.logging_utils import configure_logging


def main() -> None:
    """Entry point for the HUD companion process."""

    logger = configure_logging("kdictate.hud")

    try:
        import gi

        gi.require_version("GLib", "2.0")
        gi.require_version("Gtk", "3.0")
        from gi.repository import GLib, Gtk
    except (ImportError, ValueError) as exc:
        logger.error("GTK3 is required for the KDictate HUD: %s", exc)
        sys.exit(1)

    from kdictate.hud.window import HudWindow

    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)

    window = HudWindow(logger=logger)
    controller = HudController(
        window=window, timer=_GLibTimer(GLib), logger=logger,
    )

    bridge = HudDaemonBridge(
        on_daemon_appeared=lambda: controller.dispatch(DaemonAppeared()),
        on_daemon_vanished=lambda: controller.dispatch(DaemonVanished()),
        on_state_changed=lambda s: controller.dispatch(DaemonStateChanged(s)),
        on_partial_transcript=lambda t: controller.dispatch(PartialTranscript(t)),
        on_final_transcript=lambda t: controller.dispatch(FinalTranscript(t)),
        on_error=lambda c, m: controller.dispatch(ErrorOccurred(c, m)),
        on_snapshot=lambda *a: controller.dispatch(SnapshotReceived(*a)),
        logger=logger,
    )
    bridge.start()

    logger.info("KDictate HUD started")
    Gtk.main()


class _GLibTimer:
    """Adapt GLib.timeout_add / GLib.source_remove to the TimerScheduler protocol."""

    def __init__(self, glib_module: object) -> None:
        self._glib = glib_module

    def schedule(self, delay_ms: int, callback: Callable[[], None]) -> int:
        def _wrapper() -> bool:
            callback()
            return self._glib.SOURCE_REMOVE

        return self._glib.timeout_add(delay_ms, _wrapper)

    def cancel(self, timer_id: int) -> None:
        self._glib.source_remove(timer_id)
