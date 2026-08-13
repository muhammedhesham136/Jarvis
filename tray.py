"""
System-tray presence for JARVIS.

Hiding the window sends it here instead of quitting, so the assistant keeps
listening while the desktop is free. The tray menu restores the window,
toggles the microphone, or exits for good.

Degrades to a no-op controller when pystray/Pillow are missing, so nothing in
the UI depends on the tray actually being there — ``available`` reports which.
"""

import math
import threading

try:
    import pystray
    from pystray import Menu, MenuItem
    from PIL import Image, ImageDraw
    _TRAY_OK = True
except Exception:
    _TRAY_OK = False


CYAN = (48, 198, 255)
RED  = (255, 46, 85)


def _reactor_icon(rgb):
    """A small arc-reactor glyph in ``rgb`` on a transparent field."""
    n = 64
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)
    c   = n / 2
    r, g, b = rgb
    ring = (r, g, b, 255)
    dim  = (r, g, b, 90)

    d.ellipse((4, 4, n - 4, n - 4),     outline=ring, width=3)
    d.ellipse((16, 16, n - 16, n - 16), outline=dim,  width=2)
    d.ellipse((25, 25, n - 25, n - 25), fill=ring)
    for k in range(8):
        a  = math.radians(k * 45)
        x1, y1 = c + 15 * math.cos(a), c + 15 * math.sin(a)
        x2, y2 = c + 24 * math.cos(a), c + 24 * math.sin(a)
        d.line((x1, y1, x2, y2), fill=dim, width=2)
    return img


class Tray:
    """Non-blocking tray icon. A safe no-op when pystray is unavailable."""

    def __init__(self, on_show, on_quit, on_toggle_mute, is_muted):
        self._on_show        = on_show
        self._on_quit        = on_quit
        self._on_toggle_mute = on_toggle_mute
        self._is_muted       = is_muted
        self._icon           = None

    @property
    def available(self) -> bool:
        return _TRAY_OK

    # ── Menu wiring ───────────────────────────────────────────────────────────

    def _build(self):
        menu = Menu(
            MenuItem("Show JARVIS", self._show, default=True),
            MenuItem("Microphone muted", self._toggle_mute,
                     checked=lambda item: self._is_muted()),
            Menu.SEPARATOR,
            MenuItem("Quit JARVIS", self._quit),
        )
        self._icon = pystray.Icon("jarvis", _reactor_icon(CYAN),
                                  "J.A.R.V.I.S", menu)

    # pystray invokes callbacks as (icon, item); swallow both.
    def _show(self, *_):
        try:
            self._on_show()
        except Exception:
            pass

    def _toggle_mute(self, *_):
        try:
            self._on_toggle_mute()
        except Exception:
            pass
        self.refresh()

    def _quit(self, *_):
        try:
            if self._icon:
                self._icon.stop()
        except Exception:
            pass
        self._on_quit()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def refresh(self):
        """Recolour the glyph to match the current mute state."""
        if self._icon:
            try:
                self._icon.icon = _reactor_icon(
                    RED if self._is_muted() else CYAN)
            except Exception:
                pass

    def notify(self, message, title="J.A.R.V.I.S"):
        if self._icon:
            try:
                self._icon.notify(message, title)
            except Exception:
                pass

    def start(self):
        """Run the icon on a daemon thread and return at once."""
        if not _TRAY_OK:
            return
        self._build()
        threading.Thread(target=self._icon.run, daemon=True).start()

    def stop(self):
        if self._icon:
            try:
                self._icon.stop()
            except Exception:
                pass
