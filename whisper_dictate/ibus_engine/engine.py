"""Runtime IBus engine wiring for whisper-dictate."""

from __future__ import annotations

import logging
from types import ModuleType
from typing import Any

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib

from whisper_dictate.constants import APP_ROOT_ID, DBUS_BUS_NAME, DBUS_INTERFACE
from whisper_dictate.exceptions import IbusEngineError
from whisper_dictate.logging_utils import configure_logging
from whisper_dictate.ibus_engine.controller import DictationEngineController, EngineAdapter
from whisper_dictate.ibus_engine.dbus_client import DaemonControlBridge, DaemonSignalBridge

COMPONENT_NAME = APP_ROOT_ID
ENGINE_NAME = DBUS_INTERFACE
ENGINE_OBJECT_PATH = "/io/github/pizzimenti/WhisperDictate1/engine"
ENGINE_DESCRIPTION = "Session D-Bus driven dictation engine"
ENGINE_LONGNAME = "Whisper Dictate"
ENGINE_LANGUAGE = "en"
ENGINE_LICENSE = "MIT"
ENGINE_AUTHOR = "Bradley Pizzimenti"
ENGINE_ICON = "audio-input-microphone"
ENGINE_LAYOUT = "default"
ENGINE_VERSION = "0.3"
ENGINE_TEXTDOMAIN = "whisper-dictate"
LOGGER_NAME = "whisper_dictate.ibus"


def load_ibus_module() -> ModuleType:
    """Load the IBus typelib lazily so tests can import this package without it."""

    import gi

    gi.require_version("IBus", "1.0")
    from gi.repository import IBus  # type: ignore[import-not-found]

    return IBus


class _IbusRenderAdapter:
    """Translate controller render operations into IBus API calls."""

    def __init__(self, engine: Any, ibus_module: ModuleType) -> None:
        self._engine = engine
        self._ibus = ibus_module

    def update_preedit(self, text: str, *, visible: bool, focus_mode: str) -> None:
        ibus_text = self._ibus.Text.new_from_string(text)
        mode = (
            self._ibus.PreeditFocusMode.COMMIT
            if focus_mode == "commit"
            else self._ibus.PreeditFocusMode.CLEAR
        )
        self._engine.update_preedit_text_with_mode(ibus_text, len(text), visible, mode)
        if visible:
            self._engine.show_preedit_text()
        else:
            self._engine.hide_preedit_text()

    def commit_text(self, text: str) -> None:
        self._engine.commit_text(self._ibus.Text.new_from_string(text))


def is_toggle_shortcut(keyval: int, state: int, ibus_module: ModuleType | None = None) -> bool:
    """Return whether the key event should toggle dictation."""

    ibus = ibus_module or load_ibus_module()
    if keyval != ibus.KEY_space:
        return False
    if state & ibus.ModifierType.RELEASE_MASK:
        return False
    return bool(state & ibus.ModifierType.CONTROL_MASK)


def create_ibus_engine_class(ibus_module: ModuleType | None = None) -> type[Any]:
    """Create the concrete IBus.Engine subclass used by ibus-daemon."""

    ibus = ibus_module or load_ibus_module()
    logger = configure_logging(LOGGER_NAME)

    class WhisperDictateEngine(ibus.Engine):  # type: ignore[misc,valid-type]
        """IBus engine that mirrors daemon transcripts into preedit/commit."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._logger = logger.getChild("engine")
            self._adapter = _IbusRenderAdapter(self, ibus)
            self._controller = DictationEngineController(self._adapter, self._logger)
            self._bridge = DaemonSignalBridge(self._controller, self._logger)
            self._control = DaemonControlBridge(self._logger)
            self._bridge.start()
            self._logger.info(
                "IBus engine initialized for daemon bus %s at object path %s",
                DBUS_BUS_NAME,
                ENGINE_OBJECT_PATH,
            )

        def do_enable(self) -> None:
            self._controller.enable()

        def do_disable(self) -> None:
            self._controller.disable()

        def do_focus_in(self) -> None:
            self._controller.focus_in()

        def do_focus_out(self) -> None:
            self._controller.focus_out()

        def do_reset(self) -> None:
            self._controller.reset()

        def do_set_surrounding_text(self, text: Any, cursor_pos: int, anchor_pos: int) -> None:
            self._controller.set_surrounding_text(_coerce_text(text), cursor_pos, anchor_pos)

        def do_process_key_event(self, keyval: int, keycode: int, state: int) -> bool:
            del keycode
            if is_toggle_shortcut(keyval, state, ibus):
                self._logger.info("Ctrl+Space received by IBus engine; toggling daemon recording")
                # Dispatch via idle_add so do_process_key_event returns
                # immediately. DaemonControlBridge._call uses Gio.bus_get +
                # connection.call (fully async) so the actual D-Bus method
                # never blocks the GLib main loop today, but we keep the
                # idle_add hop so do_process_key_event stays non-blocking
                # regardless of any future change to the bridge
                # implementation. Anything that runs synchronously here
                # would freeze keyboard delivery on the KDE/Wayland desktop
                # if the daemon is slow or unreachable.
                GLib.idle_add(self._control.toggle)
                return True
            return False

        def do_destroy(self) -> None:
            self._bridge.stop()
            self._logger.info("IBus engine destroyed")
            try:
                super().do_destroy()
            except Exception:  # noqa: BLE001
                # Some IBus builds do not expose a parent do_destroy implementation.
                pass

    WhisperDictateEngine.__name__ = "WhisperDictateEngine"
    WhisperDictateEngine.__qualname__ = "WhisperDictateEngine"
    return WhisperDictateEngine



def build_engine_factory(bus: Any | None = None, ibus_module: ModuleType | None = None) -> Any:
    """Build an IBus factory that can construct the whisper-dictate engine."""

    ibus = ibus_module or load_ibus_module()
    engine_type = create_ibus_engine_class(ibus)
    active_bus = bus or ibus.Bus.new()
    try:
        factory = ibus.Factory(bus=active_bus)
    except TypeError:
        factory = ibus.Factory.new(active_bus.get_connection())
    object_path = getattr(factory, "get_object_path", None)
    if callable(object_path):
        active_path = object_path()
        expected_path = getattr(ibus, "PATH_FACTORY", None)
        if expected_path is not None and active_path != expected_path:
            raise IbusEngineError(
                f"IBus factory exported unexpected object path {active_path!r}; expected {expected_path!r}"
            )
    factory.add_engine(ENGINE_NAME, engine_type.__gtype__)
    return factory


def claim_component_name(bus: Any, ibus_module: ModuleType | None = None) -> int:
    """Claim the installed component name for the ibus-daemon launched engine."""

    ibus = ibus_module or load_ibus_module()
    request_name = getattr(bus, "request_name", None)
    if not callable(request_name):
        raise IbusEngineError("The IBus bus object does not support request_name()")

    result = request_name(COMPONENT_NAME, 0)
    if result not in {
        ibus.BusRequestNameReply.PRIMARY_OWNER,
        ibus.BusRequestNameReply.ALREADY_OWNER,
    }:
        raise IbusEngineError(f"Unable to claim IBus component name {COMPONENT_NAME!r}")
    return int(result)


def initialize_engine_runtime(
    executable_path: str,
    ibus_module: ModuleType | None = None,
) -> tuple[Any, Any]:
    """Connect to IBus and export the factory for the installed engine path."""

    del executable_path
    ibus = ibus_module or load_ibus_module()
    bus = ibus.Bus.new()
    if not bus.is_connected():
        raise IbusEngineError("Unable to connect to the IBus bus")

    factory = build_engine_factory(bus=bus, ibus_module=ibus)
    claim_component_name(bus, ibus_module=ibus)
    return bus, factory


def _coerce_text(text: Any) -> str:
    """Convert an IBus text object or plain string into a string value."""

    getter = getattr(text, "get_text", None)
    if callable(getter):
        return str(getter())
    return str(text)
