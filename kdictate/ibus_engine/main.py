"""Entry point for the kdictate IBus engine process."""

from __future__ import annotations

import logging
import sys
from typing import Any, Sequence

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib

from kdictate.exceptions import IbusEngineError
from kdictate.logging_utils import configure_logging
from kdictate.ibus_engine.engine import ENGINE_NAME, initialize_engine_runtime, load_ibus_module


def _startup_engine_runtime() -> tuple[Any, Any, Any]:
    """Load IBus and initialize the engine runtime objects."""

    ibus = load_ibus_module()
    ibus.init()
    bus, factory = initialize_engine_runtime(ibus_module=ibus)
    return ibus, bus, factory


def main(argv: Sequence[str] | None = None) -> int:
    """Run the IBus engine main loop."""

    logger = configure_logging("kdictate.ibus")
    logger.info("Starting IBus engine process for %s", ENGINE_NAME)

    try:
        ibus, bus, factory = _startup_engine_runtime()
    except IbusEngineError as exc:
        logger.error("IBus engine startup failed: %s", exc)
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected IBus engine startup failure")
        print(f"IBus engine startup failed: {exc}", file=sys.stderr)
        return 1

    loop = GLib.MainLoop()
    logger.info("IBus engine ready and entering GLib main loop")
    try:
        loop.run()
    except KeyboardInterrupt:
        logger.info("IBus engine interrupted")
    finally:
        del bus
        destroy = getattr(factory, "destroy", None)
        if callable(destroy):
            destroy()
        del factory
        del ibus

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
