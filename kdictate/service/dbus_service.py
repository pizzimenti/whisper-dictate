"""Session D-Bus service used by the dictation daemon."""

from __future__ import annotations

import logging
from typing import Any, Callable, TYPE_CHECKING

from kdictate.constants import DBUS_BUS_NAME, DBUS_INTERFACE, DBUS_OBJECT_PATH
from kdictate.exceptions import DbusServiceError, KDictateError
from kdictate.logging_utils import configure_logging
from kdictate.service.dbus_api import DBUS_INTROSPECTION_XML

if TYPE_CHECKING:
    from kdictate.core.daemon import DaemonEventSink


class SessionDbusService:
    """Expose the daemon over session D-Bus and forward daemon events as signals."""

    def __init__(
        self,
        backend: Any,
        *,
        bus_name: str = DBUS_BUS_NAME,
        object_path: str = DBUS_OBJECT_PATH,
        interface_name: str = DBUS_INTERFACE,
        logger: logging.Logger | None = None,
        signal_sender: Callable[[str, tuple[Any, ...]], None] | None = None,
    ) -> None:
        self._backend = backend
        self._bus_name = bus_name
        self._object_path = object_path
        self._interface_name = interface_name
        self._logger = logger or configure_logging("kdictate.daemon.dbus")
        self._signal_sender = signal_sender or self._default_signal_sender
        self._connection = None
        self._node_info = None
        self._interface_info = None
        self._registration_id = 0
        self._owns_bus_name = False

    def start(self) -> None:
        """Acquire the bus name and register the D-Bus object."""

        Gio, GLib = self._load_gi()
        self._connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._node_info = Gio.DBusNodeInfo.new_for_xml(DBUS_INTROSPECTION_XML)
        self._interface_info = self._node_info.interfaces[0]
        self._registration_id = self._connection.register_object(
            self._object_path,
            self._interface_info,
            self._on_method_call,
            None,
            None,
        )
        if not self._registration_id:
            raise DbusServiceError(f"Failed to register D-Bus object at {self._object_path}")

        try:
            result = self._connection.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "RequestName",
                GLib.Variant("(su)", (self._bus_name, 0)),
                GLib.VariantType("(u)"),
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
        except Exception as exc:  # noqa: BLE001
            self._connection.unregister_object(self._registration_id)
            self._registration_id = 0
            self._connection = None
            raise DbusServiceError(f"Failed to acquire D-Bus name {self._bus_name}: {exc}") from exc

        if result is None:
            self._connection.unregister_object(self._registration_id)
            self._registration_id = 0
            self._connection = None
            raise DbusServiceError(f"D-Bus RequestName returned no result for {self._bus_name}")

        (reply_code,) = result.unpack()
        # RequestName reply codes: 1 = PRIMARY_OWNER, 4 = ALREADY_OWNER.
        # Anything else (2 = IN_QUEUE, 3 = EXISTS) means another process owns
        # the name and we should not proceed.
        if reply_code not in (1, 4):
            self._connection.unregister_object(self._registration_id)
            self._registration_id = 0
            self._connection = None
            raise DbusServiceError(f"Failed to own D-Bus name {self._bus_name} (reply={reply_code})")

        self._owns_bus_name = True
        self._logger.info("owned D-Bus name %s", self._bus_name)

    def _release_bus_name(self, Gio: Any, GLib: Any) -> None:
        """Release the session bus name if it was successfully acquired."""

        if self._connection is None or not self._owns_bus_name:
            return

        try:
            self._connection.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "ReleaseName",
                GLib.Variant("(s)", (self._bus_name,)),
                GLib.VariantType("(u)"),
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("failed to release D-Bus name %s: %s", self._bus_name, exc)
        finally:
            self._owns_bus_name = False

    def stop(self) -> None:
        """Release the bus name and unregister the D-Bus object."""

        Gio, GLib = self._load_gi()
        self._release_bus_name(Gio, GLib)
        if self._connection is not None and self._registration_id:
            self._connection.unregister_object(self._registration_id)
        self._registration_id = 0
        self._connection = None

    def _load_gi(self):
        """Import Gio/GLib lazily so tests can load this module without GI."""

        try:
            import gi

            gi.require_version("Gio", "2.0")
            gi.require_version("GLib", "2.0")
            from gi.repository import Gio, GLib
        except Exception as exc:  # noqa: BLE001
            raise DbusServiceError(f"PyGObject is required for the D-Bus service: {exc}") from exc
        return Gio, GLib

    def _default_signal_sender(self, signal_name: str, parameters: tuple[Any, ...]) -> None:
        """Emit a D-Bus signal on the registered connection."""

        _Gio, GLib = self._load_gi()
        if self._connection is None:
            raise DbusServiceError("D-Bus connection is not available")

        # Daemon event callbacks arrive on background threads, but
        # Gio.DBusConnection.emit_signal must run on the GLib main loop thread.
        # GLib.idle_add schedules the emission safely without blocking the caller.
        def _emit() -> bool:
            # Re-read self._connection inside the main-loop thread: stop()
            # may have nulled it after this callback was scheduled but
            # before it ran. Without the re-check, _emit_signal_now would
            # raise AttributeError on None.emit_signal.
            connection = self._connection
            if connection is None:
                return GLib.SOURCE_REMOVE
            self._emit_signal_now(signal_name, parameters, connection, GLib)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(_emit)

    def _emit_signal_now(
        self,
        signal_name: str,
        parameters: tuple[Any, ...],
        connection: Any,
        GLib: Any,
    ) -> None:
        """Emit a signal immediately on the supplied connection."""

        if signal_name in {"StateChanged", "PartialTranscript", "FinalTranscript"}:
            variant = GLib.Variant("(s)", parameters)
        elif signal_name == "ErrorOccurred":
            variant = GLib.Variant("(ss)", parameters)
        else:
            raise DbusServiceError(f"Unsupported signal: {signal_name}")
        connection.emit_signal(
            None,
            self._object_path,
            self._interface_name,
            signal_name,
            variant,
        )
        if signal_name in {"PartialTranscript", "FinalTranscript"}:
            self._logger.info("signal emitted: %s [REDACTED]", signal_name)
        else:
            self._logger.info("signal emitted: %s %s", signal_name, parameters)

    def state_changed(self, state: str) -> None:
        """Publish a state transition."""

        self._signal_sender("StateChanged", (state,))

    def partial_transcript(self, text: str) -> None:
        """Publish a partial transcript for IBus preedit consumers."""

        self._signal_sender("PartialTranscript", (text,))

    def final_transcript(self, text: str) -> None:
        """Publish a finalized transcript for IBus commit consumers."""

        self._signal_sender("FinalTranscript", (text,))

    def error_occurred(self, code: str, message: str) -> None:
        """Publish a structured error signal."""

        self._signal_sender("ErrorOccurred", (code, message))

    def _dispatch(self, method_name: str) -> tuple[Any, ...]:
        """Invoke a backend method and normalize the return shape."""

        method_map = {
            "Start": getattr(self._backend, "request_start", getattr(self._backend, "start", None)),
            "Stop": getattr(self._backend, "request_stop", getattr(self._backend, "stop", None)),
            "Toggle": getattr(self._backend, "toggle", None),
            "GetState": getattr(self._backend, "get_state", None),
            "GetLastText": getattr(self._backend, "get_last_text", None),
            "Ping": getattr(self._backend, "ping", None),
        }
        handler = method_map.get(method_name)
        if handler is None:
            raise DbusServiceError(f"Unsupported method: {method_name}")
        result = handler()
        if method_name in {"GetState", "GetLastText", "Ping"}:
            return ("" if result is None else result,)
        return ()

    def _on_method_call(
        self,
        connection: Any,
        sender: str,
        object_path: str,
        interface_name: str,
        method_name: str,
        parameters: Any,
        invocation: Any,
        user_data: Any = None,
    ) -> None:
        """Handle incoming D-Bus method calls."""

        # Security note: this is a same-user session bus. The `sender`
        # peer name is intentionally discarded — any process running as
        # the user can already do anything the user can, so per-peer
        # access control here would not raise the privilege bar. If you
        # ever expose this service on the system bus or to a sandboxed
        # peer (Flatpak portal, etc.), reintroduce a sender allowlist.
        del connection, sender, object_path, interface_name, parameters, user_data
        Gio, GLib = self._load_gi()
        try:
            result = self._dispatch(method_name)
            if result:
                invocation.return_value(GLib.Variant("(s)", result))
            else:
                invocation.return_value(None)
        except KDictateError as exc:
            self.error_occurred("dbus_method_failed", str(exc))
            invocation.return_dbus_error(f"{self._bus_name}.Error", str(exc))
        except Exception as exc:  # noqa: BLE001
            self.error_occurred("dbus_method_failed", str(exc))
            self._logger.exception("unexpected D-Bus method failure")
            invocation.return_dbus_error(f"{self._bus_name}.Error", str(exc))
