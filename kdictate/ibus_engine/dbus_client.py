"""Session D-Bus bridge for daemon transcript events and control calls."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib

from kdictate.constants import DBUS_BUS_NAME, DBUS_INTERFACE, DBUS_OBJECT_PATH
from kdictate.ibus_engine.controller import DictationEngineController

DBUS_SIGNAL_NAMES: tuple[str, ...] = (
    "StateChanged",
    "PartialTranscript",
    "FinalTranscript",
    "ErrorOccurred",
)


@dataclass(slots=True)
class _Subscription:
    """Track one active signal subscription."""

    connection: Any
    subscription_ids: list[int] = field(default_factory=list)


class DaemonSignalBridge:
    """Watch the daemon's session bus name and forward transcript signals."""

    def __init__(
        self,
        controller: DictationEngineController,
        logger: logging.Logger,
        *,
        bus_name: str = DBUS_BUS_NAME,
        object_path: str = DBUS_OBJECT_PATH,
        interface_name: str = DBUS_INTERFACE,
        bus_type: Gio.BusType = Gio.BusType.SESSION,
        watch_name: Callable[..., int] | None = None,
        unwatch_name: Callable[[int], None] | None = None,
    ) -> None:
        self._controller = controller
        self._logger = logger
        self._bus_name = bus_name
        self._object_path = object_path
        self._interface_name = interface_name
        self._bus_type = bus_type
        self._watch_name = watch_name or Gio.bus_watch_name
        self._unwatch_name = unwatch_name or Gio.bus_unwatch_name
        self._watch_id: int | None = None
        self._subscription: _Subscription | None = None
        self._seed_generation: int = 0

    def start(self) -> None:
        """Start watching the daemon bus name."""

        if self._watch_id is not None:
            return

        self._watch_id = self._watch_name(
            self._bus_type,
            self._bus_name,
            Gio.BusNameWatcherFlags.NONE,
            self._on_name_appeared,
            self._on_name_vanished,
        )
        self._logger.info("Watching daemon bus name %s", self._bus_name)

    def stop(self) -> None:
        """Stop watching the daemon bus name and clear subscriptions."""

        self._unsubscribe()
        if self._watch_id is None:
            return

        self._unwatch_name(self._watch_id)
        self._watch_id = None
        self._logger.info("Stopped watching daemon bus name %s", self._bus_name)

    def _on_name_appeared(self, connection: Gio.DBusConnection, name: str, owner: str) -> None:
        """Subscribe to transcript signals once the daemon is present."""

        del name, owner
        self._logger.info("Daemon bus name appeared")
        self._controller.set_daemon_available(True)
        self._subscribe(connection)
        self._seed_state(connection)

    def _on_name_vanished(self, connection: Gio.DBusConnection, name: str) -> None:
        """Clear subscriptions and stale UI state when the daemon disappears."""

        del connection, name
        self._logger.warning("Daemon bus name vanished")
        self._controller.set_daemon_available(False)
        self._unsubscribe()

    def _subscribe(self, connection: Gio.DBusConnection) -> None:
        """Subscribe to the daemon's transcript signals."""

        self._unsubscribe()
        self._seed_generation += 1
        subscription_ids: list[int] = []
        for signal_name in DBUS_SIGNAL_NAMES:
            subscription_ids.append(
                connection.signal_subscribe(
                    self._bus_name,
                    self._interface_name,
                    signal_name,
                    self._object_path,
                    None,
                    Gio.DBusSignalFlags.NONE,
                    self._on_signal,
                )
            )
        self._subscription = _Subscription(connection=connection, subscription_ids=subscription_ids)
        self._logger.info("Subscribed to daemon transcript signals")

    def _unsubscribe(self) -> None:
        """Remove any active signal subscriptions."""

        if self._subscription is None:
            return

        for subscription_id in self._subscription.subscription_ids:
            self._subscription.connection.signal_unsubscribe(subscription_id)
        self._subscription = None
        self._logger.debug("Unsubscribed from daemon transcript signals")

    def _seed_state(self, connection: Gio.DBusConnection) -> None:
        """Query the daemon state asynchronously so reconnects are deterministic.

        This is called from _on_name_appeared, which runs on the GLib main loop
        thread.  Using call() (async) instead of call_sync() avoids blocking the
        entire event loop — and therefore all IBus preedit updates — while the
        daemon responds.
        """

        generation = self._seed_generation

        def _on_reply(source: Any, result: Gio.AsyncResult, user_data: object) -> None:
            if self._subscription is None or generation != self._seed_generation:
                self._logger.debug(
                    "Dropping stale GetState reply (generation %d, current %d)",
                    generation, self._seed_generation,
                )
                return
            try:
                reply = connection.call_finish(result)
            except Exception as exc:  # noqa: BLE001
                self._logger.warning("GetState failed while seeding daemon state: %s", exc)
                return

            if reply is None:
                return

            try:
                (state,) = reply.unpack()
            except Exception as exc:  # noqa: BLE001
                self._logger.warning("Could not unpack GetState reply: %s", exc)
                return

            self._controller.handle_state_changed(str(state))

        connection.call(
            self._bus_name,
            self._object_path,
            self._interface_name,
            "GetState",
            None,
            GLib.VariantType("(s)"),
            Gio.DBusCallFlags.NONE,
            5000,
            None,
            _on_reply,
            None,
        )

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
        """Dispatch a D-Bus signal from the daemon to the controller."""

        del connection, sender_name, object_path, interface_name, user_data
        try:
            values = parameters.unpack()
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("Failed to unpack daemon signal %s: %s", signal_name, exc)
            return

        if signal_name == "StateChanged" and len(values) == 1:
            self._controller.handle_state_changed(str(values[0]))
        elif signal_name == "PartialTranscript" and len(values) == 1:
            self._controller.handle_partial_transcript(str(values[0]))
        elif signal_name == "FinalTranscript" and len(values) == 1:
            self._controller.handle_final_transcript(str(values[0]))
        elif signal_name == "ErrorOccurred" and len(values) == 2:
            self._controller.handle_error(str(values[0]), str(values[1]))
        else:
            self._logger.warning("Ignoring malformed daemon signal %s with payload %r", signal_name, values)


class DaemonControlBridge:
    """Issue control requests to the daemon over the session bus."""

    def __init__(
        self,
        logger: logging.Logger,
        *,
        bus_name: str = DBUS_BUS_NAME,
        object_path: str = DBUS_OBJECT_PATH,
        interface_name: str = DBUS_INTERFACE,
        bus_type: Gio.BusType = Gio.BusType.SESSION,
        bus_get: Callable[..., None] | None = None,
        bus_get_finish: Callable[..., Any] | None = None,
    ) -> None:
        self._logger = logger
        self._bus_name = bus_name
        self._object_path = object_path
        self._interface_name = interface_name
        self._bus_type = bus_type
        # Use fully async bus_get / call so that _call() never blocks the GLib
        # main loop thread, even when invoked from a GLib.idle_add callback.
        self._bus_get = bus_get or Gio.bus_get
        self._bus_get_finish = bus_get_finish or Gio.bus_get_finish

    def toggle(self) -> None:
        """Toggle recording state on the daemon."""

        self._call("Toggle")

    def _call(self, method_name: str) -> None:
        def _on_connection(source: Any, result: Any, user_data: object) -> None:
            try:
                connection = self._bus_get_finish(result)
            except Exception as exc:  # noqa: BLE001
                self._logger.warning("Daemon control call %s: bus_get failed: %s", method_name, exc)
                return

            def _on_reply(source: Any, result: Any, user_data: object) -> None:
                try:
                    connection.call_finish(result)
                except Exception as exc:  # noqa: BLE001
                    self._logger.warning("Daemon control call %s failed: %s", method_name, exc)

            connection.call(
                self._bus_name,
                self._object_path,
                self._interface_name,
                method_name,
                None,
                None,
                Gio.DBusCallFlags.NONE,
                5000,
                None,
                _on_reply,
                None,
            )

        self._bus_get(self._bus_type, None, _on_connection, None)
