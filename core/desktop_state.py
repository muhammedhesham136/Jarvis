"""
Awareness of what is already open on the desktop.

Shared by every tool so a task is continued rather than restarted: "open
WhatsApp" then "message Sara" reuses the window JARVIS already opened, "open
Spotify" then "play a song" acts on the running Spotify, and so on for any
application. The truth comes from the live window list, so it stays correct
even for things the user opened by hand.

Everything here is best-effort and degrades to a safe default when the window
or process libraries are missing — a tool then simply behaves as it did before,
opening fresh.
"""

import platform
import threading
import time

try:
    import pygetwindow as gw
    _GW = True
except Exception:
    _GW = False

try:
    import psutil
    _PS = True
except Exception:
    _PS = False


_LOCK   = threading.Lock()
_OPENED = {}   # app key -> monotonic time JARVIS last opened/focused it

# Substrings that identify an app in a window title or process name. An app not
# listed here falls back to matching on its own name, which covers most cases.
_HINTS = {
    "whatsapp":  ["whatsapp"],
    "telegram":  ["telegram"],
    "discord":   ["discord"],
    "slack":     ["slack"],
    "spotify":   ["spotify"],
    "instagram": ["instagram"],
    "brave":     ["brave"],
    "chrome":    ["chrome", "google chrome"],
    "edge":      ["edge"],
    "firefox":   ["firefox", "mozilla"],
    "vscode":    ["visual studio code"],
    "code":      ["visual studio code"],
    "notepad":   ["notepad"],
    "explorer":  ["file explorer"],
    "word":      ["word"],
    "excel":     ["excel"],
    "powerpoint":["powerpoint"],
    "steam":     ["steam"],
    "vlc":       ["vlc"],
    "zoom":      ["zoom"],
    "notion":    ["notion"],
    "obsidian":  ["obsidian"],
}


def _key(app: str) -> str:
    return (app or "").strip().lower()


def _hints_for(app: str) -> list:
    k = _key(app)
    if k in _HINTS:
        return _HINTS[k]
    # Fall back to the app's own words, minus a trailing ".exe".
    base = k.replace(".exe", "").strip()
    return [base] if base else []


def note_opened(app: str) -> None:
    """Record that JARVIS just opened or focused an app."""
    with _LOCK:
        _OPENED[_key(app)] = time.monotonic()


def _find_window(app: str):
    """A visible top-level window matching the app, or None."""
    if not _GW:
        return None
    hints = _hints_for(app)
    if not hints:
        return None
    try:
        for w in gw.getAllWindows():
            title = (getattr(w, "title", "") or "").lower()
            if not title:
                continue
            if any(h in title for h in hints):
                return w
    except Exception:
        pass
    return None


def _process_running(app: str) -> bool:
    if not _PS:
        return False
    hints = _hints_for(app)
    if not hints:
        return False
    try:
        for proc in psutil.process_iter(["name"]):
            name = (proc.info.get("name") or "").lower()
            if name and any(h.replace(" ", "") in name.replace(" ", "") for h in hints):
                return True
    except Exception:
        pass
    return False


def is_open(app: str) -> bool:
    """True if the app has a window or a running process right now."""
    return _find_window(app) is not None or _process_running(app)


def focus(app: str) -> bool:
    """Bring an already-open app to the front. False if it is not open."""
    w = _find_window(app)
    if w is None:
        return False

    hwnd = getattr(w, "_hWnd", None)
    if hwnd and platform.system() == "Windows":
        try:
            import ctypes
            user32 = ctypes.windll.user32
            user32.ShowWindow(hwnd, 9)          # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            note_opened(app)
            return True
        except Exception:
            pass

    try:
        if getattr(w, "isMinimized", False):
            w.restore()
        w.activate()
        note_opened(app)
        return True
    except Exception:
        # activate() is flaky on Windows; the minimise/restore bounce forces it.
        try:
            w.minimize()
            w.restore()
            note_opened(app)
            return True
        except Exception:
            return False


def open_windows(limit: int = 12) -> list:
    """De-duplicated titles of the visible windows, for telling the model
    where things stand."""
    if not _GW:
        return []
    out, seen = [], set()
    try:
        for w in gw.getAllWindows():
            title = (getattr(w, "title", "") or "").strip()
            if not title or title.lower() in seen:
                continue
            if not getattr(w, "visible", True):
                continue
            seen.add(title.lower())
            out.append(title)
            if len(out) >= limit:
                break
    except Exception:
        pass
    return out


def snapshot() -> str:
    """One line naming what is open, or '' when nothing useful is known."""
    wins = open_windows()
    if not wins:
        return ""
    return "Open on the desktop right now: " + "; ".join(wins) + "."
