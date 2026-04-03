"""Session D-Bus service used by the dictation daemon."""

from __future__ import annotations

import logging
from typing import Any, Callable, TYPE_CHECKING

from whisper_dictate.constants import DBUS_BUS_NAME, DBUS_INTERFACE, DBUS_OBJECT_PATH
from whisper_dictate.exceptions import DbusServiceError, WhisperDictateError
from whisper_dictate.logging_utils import configure_logging
from whisper_dictate.service.dbus_api import DBUS_INTROSPECTION_XML

if TYPE_CHECKING:
    from whisper_dictate.core.daemon import DaemonEventSink


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
        self._logger = logger or configure_logging("whisper_dictate.dbus")
        self._signal_sender = signal_sender or self._default_signal_sender
        self._connection = None
        self._node_info = None
        self._interface_info = None
        self._registration_id = 0
        self._owner_id = 0

    def start(self) -> None:
        """Acquire the bus name and register the D-Bus object."""

        Gio, _GLib = self._load_gi()
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
        self._owner_id = Gio.bus_own_name_on_connection(
            self._connection,
            self._bus_name,
            Gio.BusNameOwnerFlags.NONE,
            None,
            None,
        )
        self._logger.info("owned D-Bus name %s", self._bus_name)

    def stop(self) -> None:
        """Release the bus name and unregister the D-Bus object."""

        Gio, _GLib = self._load_gi()
        if self._connection is not None and self._registration_id:
            self._connection.unregister_object(self._registration_id)
        if self._owner_id:
            Gio.bus_unown_name(self._owner_id)
        self._registration_id = 0
        self._owner_id = 0
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

        Gio, GLib = self._load_gi()
        if self._connection is None:
            raise DbusServiceError("D-Bus connection is not available")
        def _emit() -> bool:
            self._emit_signal_now(signal_name, parameters, Gio, GLib)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(_emit)

    def _emit_signal_now(self, signal_name: str, parameters: tuple[Any, ...], Gio: Any, GLib: Any) -> None:
        """Emit a signal immediately on the active connection."""

        if signal_name in {"StateChanged", "PartialTranscript", "FinalTranscript"}:
            variant = GLib.Variant("(s)", parameters)
        elif signal_name == "ErrorOccurred":
            variant = GLib.Variant("(ss)", parameters)
        else:
            raise DbusServiceError(f"Unsupported signal: {signal_name}")
        self._connection.emit_signal(
            None,
            self._object_path,
            self._interface_name,
            signal_name,
            variant,
        )
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

        del connection, sender, object_path, interface_name, parameters, user_data
        Gio, GLib = self._load_gi()
        try:
            result = self._dispatch(method_name)
            if result:
                invocation.return_value(GLib.Variant("(s)", result))
            else:
                invocation.return_value(None)
        except WhisperDictateError as exc:
            self.error_occurred("dbus_method_failed", str(exc))
            invocation.return_dbus_error(f"{self._bus_name}.Error", str(exc))
        except Exception as exc:  # noqa: BLE001
            self.error_occurred("dbus_method_failed", str(exc))
            self._logger.exception("unexpected D-Bus method failure")
            invocation.return_dbus_error(f"{self._bus_name}.Error", str(exc))
