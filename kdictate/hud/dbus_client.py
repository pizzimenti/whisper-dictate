"""D-Bus bridge connecting the HUD to the kdictate daemon via Gio."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib

from kdictate.constants import DBUS_BUS_NAME, DBUS_INTERFACE, DBUS_OBJECT_PATH

DBUS_SIGNAL_NAMES: tuple[str, ...] = (
    "StateChanged",
    "PartialTranscript",
    "FinalTranscript",
    "ErrorOccurred",
)


@dataclass(slots=True)
class _Subscription:
    """Track active signal subscriptions on one connection."""

    connection: Any
    subscription_ids: list[int] = field(default_factory=list)


class HudDaemonBridge:
    """Watch the daemon bus name, subscribe to signals, and forward events.

    Mirrors the IBus engine's ``DaemonSignalBridge`` but dispatches to
    plain callbacks instead of a controller object.
    """

    def __init__(
        self,
        *,
        on_daemon_appeared: Callable[[], None],
        on_daemon_vanished: Callable[[], None],
        on_state_changed: Callable[[str], None],
        on_partial_transcript: Callable[[str], None],
        on_final_transcript: Callable[[str], None],
        on_error: Callable[[str, str], None],
        on_snapshot: Callable[[str, str, str, str, str], None],
        logger: logging.Logger | None = None,
        bus_type: Gio.BusType = Gio.BusType.SESSION,
        watch_name: Callable[..., int] | None = None,
        unwatch_name: Callable[[int], None] | None = None,
    ) -> None:
        self._logger = logger or logging.getLogger("kdictate.hud.dbus")
        self._bus_type = bus_type
        self._watch_name = watch_name or Gio.bus_watch_name
        self._unwatch_name = unwatch_name or Gio.bus_unwatch_name

        self._on_daemon_appeared = on_daemon_appeared
        self._on_daemon_vanished = on_daemon_vanished
        self._on_state_changed = on_state_changed
        self._on_partial_transcript = on_partial_transcript
        self._on_final_transcript = on_final_transcript
        self._on_error = on_error
        self._on_snapshot = on_snapshot

        self._watch_id: int | None = None
        self._subscription: _Subscription | None = None
        self._seed_generation: int = 0

    def start(self) -> None:
        """Start watching the daemon bus name."""

        if self._watch_id is not None:
            return

        self._watch_id = self._watch_name(
            self._bus_type,
            DBUS_BUS_NAME,
            Gio.BusNameWatcherFlags.NONE,
            self._on_name_appeared,
            self._on_name_vanished,
        )
        self._logger.info("Watching daemon bus name %s", DBUS_BUS_NAME)

    def stop(self) -> None:
        """Stop watching and clear subscriptions."""

        self._unsubscribe()
        if self._watch_id is None:
            return
        self._unwatch_name(self._watch_id)
        self._watch_id = None
        self._logger.info("Stopped watching daemon bus name")

    # -- Name watcher callbacks ---------------------------------------------

    def _on_name_appeared(
        self, connection: Gio.DBusConnection, name: str, owner: str,
    ) -> None:
        del name, owner
        self._logger.info("Daemon appeared on session bus")
        self._subscribe(connection)
        self._on_daemon_appeared()
        self._seed_state(connection)

    def _on_name_vanished(
        self, connection: Gio.DBusConnection, name: str,
    ) -> None:
        del connection, name
        self._logger.warning("Daemon vanished from session bus")
        self._unsubscribe()
        self._on_daemon_vanished()

    # -- Signal subscription ------------------------------------------------

    def _subscribe(self, connection: Gio.DBusConnection) -> None:
        self._unsubscribe()
        self._seed_generation += 1
        subscription_ids: list[int] = []
        for signal_name in DBUS_SIGNAL_NAMES:
            subscription_ids.append(
                connection.signal_subscribe(
                    DBUS_BUS_NAME,
                    DBUS_INTERFACE,
                    signal_name,
                    DBUS_OBJECT_PATH,
                    None,
                    Gio.DBusSignalFlags.NONE,
                    self._on_signal,
                )
            )
        self._subscription = _Subscription(
            connection=connection, subscription_ids=subscription_ids,
        )
        self._logger.info("Subscribed to daemon D-Bus signals")

    def _unsubscribe(self) -> None:
        if self._subscription is None:
            return
        for sub_id in self._subscription.subscription_ids:
            self._subscription.connection.signal_unsubscribe(sub_id)
        self._subscription = None
        self._logger.debug("Unsubscribed from daemon D-Bus signals")

    def _on_signal(
        self,
        connection: Gio.DBusConnection,
        sender_name: str,
        object_path: str,
        interface_name: str,
        signal_name: str,
        parameters: GLib.Variant,
        user_data: object | None = None,
    ) -> None:
        del connection, sender_name, object_path, interface_name, user_data
        try:
            values = parameters.unpack()
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("Failed to unpack signal %s: %s", signal_name, exc)
            return

        if signal_name == "StateChanged" and len(values) == 1:
            self._on_state_changed(str(values[0]))
        elif signal_name == "PartialTranscript" and len(values) == 1:
            self._on_partial_transcript(str(values[0]))
        elif signal_name == "FinalTranscript" and len(values) == 1:
            self._on_final_transcript(str(values[0]))
        elif signal_name == "ErrorOccurred" and len(values) == 2:
            self._on_error(str(values[0]), str(values[1]))
        else:
            self._logger.warning(
                "Ignoring malformed signal %s with payload %r", signal_name, values,
            )

    # -- State seeding ------------------------------------------------------

    def _seed_state(self, connection: Gio.DBusConnection) -> None:
        """Query daemon snapshot asynchronously so the HUD starts in sync."""

        generation = self._seed_generation

        def _on_reply(source: Any, result: Gio.AsyncResult, user_data: object) -> None:
            if self._subscription is None or generation != self._seed_generation:
                self._logger.debug(
                    "Dropping stale GetSnapshot reply (generation %d, current %d)",
                    generation, self._seed_generation,
                )
                return
            try:
                reply = connection.call_finish(result)
            except Exception as exc:  # noqa: BLE001
                self._logger.warning("GetSnapshot seed failed: %s", exc)
                return
            if reply is None:
                return
            try:
                values = reply.unpack()
            except Exception as exc:  # noqa: BLE001
                self._logger.warning("Could not unpack GetSnapshot reply: %s", exc)
                return
            if len(values) == 5:
                self._on_snapshot(*(str(v) for v in values))
            else:
                self._logger.warning("GetSnapshot returned %d values, expected 5", len(values))

        connection.call(
            DBUS_BUS_NAME,
            DBUS_OBJECT_PATH,
            DBUS_INTERFACE,
            "GetSnapshot",
            None,
            GLib.VariantType("(sssss)"),
            Gio.DBusCallFlags.NONE,
            5000,
            None,
            _on_reply,
            None,
        )
