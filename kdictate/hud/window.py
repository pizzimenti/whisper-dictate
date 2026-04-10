"""Non-focusable GTK3 overlay window for the KDictate HUD.

GTK is imported lazily inside ``HudWindow.__init__`` so the module can be
loaded without a GTK3 typelib present (the caller handles the error).
"""

from __future__ import annotations

import logging

from kdictate.hud.view_model import HudPresentation

MARGIN = 12
WINDOW_MIN_WIDTH = 220
WINDOW_MAX_WIDTH = 420

_CSS_TEMPLATE = """\
#kdictate-hud {{
    background-color: {bg};
    color: {fg};
    border-radius: 8px;
    padding: 8px 14px;
    font-size: 13px;
    min-width: {min_w}px;
    max-width: {max_w}px;
}}
"""

_STYLES: dict[str, tuple[str, str]] = {
    "neutral":  ("rgba(30, 30, 30, 0.82)",  "#cccccc"),
    "active":   ("rgba(20, 80, 180, 0.86)",  "#ffffff"),
    "success":  ("rgba(30, 120, 50, 0.86)",  "#ffffff"),
    "error":    ("rgba(180, 40, 40, 0.86)",  "#ffffff"),
}


class HudWindow:
    """Lightweight overlay that displays HUD feedback without stealing focus.

    Uses a GTK3 popup window with ``set_accept_focus(False)`` and
    ``set_keep_above(True)`` so it never becomes the active window.

    Placement targets the bottom-right of the primary monitor workarea
    (tray-adjacent for a standard bottom Plasma panel).  On Wayland this
    is best-effort screen-corner positioning -- see future.md.
    """

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        import gi

        gi.require_version("Gdk", "3.0")
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gdk, Gtk

        self._Gdk = Gdk
        self._Gtk = Gtk
        self._logger = logger or logging.getLogger("kdictate.hud.window")
        self._current_style = ""

        self._window = Gtk.Window(type=Gtk.WindowType.POPUP)
        self._window.set_decorated(False)
        self._window.set_keep_above(True)
        self._window.set_accept_focus(False)
        self._window.set_skip_taskbar_hint(True)
        self._window.set_skip_pager_hint(True)
        self._window.set_resizable(False)

        # Enable RGBA visuals for translucent background.
        screen = self._window.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None:
            self._window.set_visual(visual)
        self._window.set_app_paintable(True)

        self._label = Gtk.Label()
        self._label.set_name("kdictate-hud")
        self._label.set_line_wrap(True)
        self._label.set_xalign(0.0)
        self._window.add(self._label)

        self._css_provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider(
            self._label.get_style_context(),
            self._css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        self._apply_style("neutral")

    def update_presentation(self, presentation: HudPresentation) -> None:
        """Apply a new presentation snapshot to the window."""

        self._apply_style(presentation.style)
        self._label.set_text(presentation.label)

        if presentation.visible:
            self._window.show_all()
            self._reposition()
        else:
            self._window.hide()

    def _apply_style(self, style: str) -> None:
        if style == self._current_style:
            return
        self._current_style = style
        bg, fg = _STYLES.get(style, _STYLES["neutral"])
        css = _CSS_TEMPLATE.format(
            bg=bg, fg=fg, min_w=WINDOW_MIN_WIDTH, max_w=WINDOW_MAX_WIDTH,
        )
        self._css_provider.load_from_data(css.encode("utf-8"))

    def _reposition(self) -> None:
        """Place the window at the bottom-right of the primary monitor."""

        Gdk = self._Gdk
        display = Gdk.Display.get_default()
        if display is None:
            return

        monitor = display.get_primary_monitor()
        if monitor is None and display.get_n_monitors() > 0:
            monitor = display.get_monitor(0)
        if monitor is None:
            return

        workarea = monitor.get_workarea()

        # Use preferred size rather than current allocation so the first
        # placement (before a size-allocate cycle) is correct.
        _min_w, nat_w = self._window.get_preferred_width()
        _min_h, nat_h = self._window.get_preferred_height()
        w = max(nat_w, WINDOW_MIN_WIDTH)
        h = nat_h

        x = workarea.x + workarea.width - w - MARGIN
        y = workarea.y + workarea.height - h - MARGIN
        self._window.move(x, y)

    @property
    def visible(self) -> bool:
        """Whether the overlay is currently shown."""
        return self._window.get_visible()
