"""kdictate package."""

from .app_metadata import APP_VERSION
from .constants import (
    APP_ROOT_ID,
    CANONICAL_STATES,
    DBUS_BUS_NAME,
    DBUS_INTERFACE,
    DBUS_OBJECT_PATH,
)

__version__ = APP_VERSION

__all__ = [
    "__version__",
    "APP_ROOT_ID",
    "CANONICAL_STATES",
    "DBUS_BUS_NAME",
    "DBUS_INTERFACE",
    "DBUS_OBJECT_PATH",
]
