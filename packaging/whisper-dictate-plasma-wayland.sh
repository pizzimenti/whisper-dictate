#!/bin/sh

# On KDE Plasma Wayland, native clients should use the compositor-backed
# input-method path instead of forcing toolkit-specific IBus modules.
unset GTK_IM_MODULE
unset QT_IM_MODULE
