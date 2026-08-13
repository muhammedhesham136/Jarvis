"""
J.A.R.V.I.S — 3D WebGL front end.

The reactor is drawn by ``ui_web/index.html`` inside a frameless fullscreen
WebView2 window. Python and the page talk over a loopback WebSocket rather
than through ``evaluate_js``: the socket runs on its own event loop, so
pushing 30 state frames a second never touches — and never stalls — the
window's UI thread or the assistant's audio threads.

If pywebview or WebView2 is unavailable the module transparently falls back
to the Tk renderer in ``ui_tk.py``; the assistant itself is unaffected.

Public surface used by main.py and the action modules:

    ui.muted              bool, read by the mic loop
    ui.on_text_command    callback(str) for typed orders
    ui.set_state(str)     LISTENING | SPEAKING | THINKING | PROCESSING | MUTED
    ui.set_level(float)   live audio energy, 0..1, safe from any thread
    ui.write_log(str)     safe from any thread
    ui.wait_for_api_key()
    ui.start()            blocks — must be called on the main thread
"""

import asyncio
import functools
import json
import os
import socket
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tray import Tray


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR   = get_base_dir()
CONFIG_DIR = BASE_DIR / "config"
API_FILE   = CONFIG_DIR / "api_keys.json"
PAGE       = BASE_DIR / "ui_web" / "index.html"

try:
    import webview
    import websockets
    from websockets.asyncio.server import serve as ws_serve
    _WEB_OK = True
except Exception as _e:                                    # pragma: no cover
    # stderr, flushed, ASCII: stdout is block-buffered once redirected and the
    # Tk fallback leaves through os._exit(), which never flushes it — a message
    # on stdout can be scrolled away by the later imports or lost outright.
    print(
        "\n" + "=" * 72 + "\n"
        "  JARVIS IS RUNNING WITHOUT THE 3D WebGL INTERFACE\n"
        + "-" * 72 + "\n"
        f"  reason      : {_e}\n"
        f"  python used : {sys.executable}\n"
        "\n"
        "  FIX: quit and start JARVIS with  run.bat  -- it launches\n"
        "       .venv\\Scripts\\python.exe, where the WebGL packages are.\n"
        "\n"
        "  If the interpreter above is already the one you want, then that\n"
        "  package really is missing from it -- install it there:\n"
        f'      "{sys.executable}" -m pip install pywebview\n'
        "      (or -m pip install -r requirements.txt for all of them)\n"
        + "=" * 72 + "\n",
        file=sys.stderr, flush=True,
    )
    _WEB_OK = False


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _QuietFiles(SimpleHTTPRequestHandler):
    """Serves ui_web/ and keeps its request logging to itself."""

    def log_message(self, *args):
        pass


class _WebJarvisUI:

    STATES = {"LISTENING", "SPEAKING", "THINKING", "PROCESSING",
              "MUTED", "INITIALISING"}

    def __init__(self, face_path=None, size=None):
        self.muted           = False
        self.speaking        = False
        self.on_text_command = None

        self._state = "INITIALISING"
        self._level = 0.0
        self._api_key_ready = API_FILE.exists()

        # Tray / background state.
        self._quitting     = False   # True only for a real exit
        self._tray_notified = False  # one-shot "still listening" balloon
        self._tray = Tray(
            on_show=self._show_window,
            on_quit=self._quit,
            on_toggle_mute=self.toggle_mute,
            is_muted=lambda: self.muted,
        )

        self._clients: set = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._port = _free_port()
        self._ready = threading.Event()

        threading.Thread(target=self._serve, daemon=True).start()
        self._ready.wait(timeout=5.0)

        # The page is served over loopback rather than opened as a file:// URL.
        # WebView2 will not accept a file URL carrying a query string, and this
        # also sidesteps percent-encoding of spaces in the project path.
        self._http_port = _free_port()
        httpd = ThreadingHTTPServer(
            ("127.0.0.1", self._http_port),
            functools.partial(_QuietFiles, directory=str(PAGE.parent)),
        )
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        self._window = webview.create_window(
            "J.A.R.V.I.S",
            url=f"http://127.0.0.1:{self._http_port}/index.html?port={self._port}",
            fullscreen=True,
            frameless=True,
            background_color="#000000",
            easy_drag=False,
        )

        # Native close (Alt+F4 / WM_CLOSE) hides to the tray rather than
        # exiting — but only if the tray is actually there to bring it back.
        try:
            self._window.events.closing += self._on_closing
        except Exception:
            pass

    # ── Socket link ───────────────────────────────────────────────────────────

    def _serve(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._serve_async())
        except Exception as e:
            print(f"[UI] link down: {e}")

    async def _serve_async(self):
        async def handler(ws):
            self._clients.add(ws)
            try:
                if not self._api_key_ready:
                    await ws.send(json.dumps({"type": "need_key"}))
                async for raw in ws:
                    try:
                        self._handle(json.loads(raw))
                    except Exception as e:
                        print(f"[UI] bad message: {e}")
            except Exception:
                pass
            finally:
                self._clients.discard(ws)

        async with ws_serve(handler, "127.0.0.1", self._port,
                            ping_interval=None, compression=None):
            self._ready.set()
            await self._push_loop()

    async def _push_loop(self):
        """Stream state to the page at 30 Hz. Tiny payload, fixed cost."""
        while True:
            if self._clients:
                msg = json.dumps({
                    "type":  "state",
                    "state": "MUTED" if self.muted else self._state,
                    "level": round(self._level, 3),
                    "muted": self.muted,
                })
                await asyncio.gather(
                    *(c.send(msg) for c in list(self._clients)),
                    return_exceptions=True,
                )
            await asyncio.sleep(1 / 30)

    def _send_now(self, obj: dict):
        if not self._loop:
            return
        msg = json.dumps(obj)

        async def _fan():
            await asyncio.gather(*(c.send(msg) for c in list(self._clients)),
                                 return_exceptions=True)

        try:
            asyncio.run_coroutine_threadsafe(_fan(), self._loop)
        except Exception:
            pass

    # ── Messages from the page ────────────────────────────────────────────────

    def _handle(self, m: dict):
        kind = m.get("type")

        if kind == "cmd":
            text = (m.get("text") or "").strip()
            if text and self.on_text_command:
                threading.Thread(target=self.on_text_command,
                                 args=(text,), daemon=True).start()

        elif kind == "mute":
            self.muted = bool(m.get("value"))
            self._tray.refresh()
            self.write_log("SYS: Microphone muted." if self.muted
                           else "SYS: Microphone live.")

        elif kind == "key":
            self._save_key((m.get("value") or "").strip())

        elif kind == "hide":
            self._hide_window()

        elif kind == "quit":
            self._quit()

    def _save_key(self, key: str):
        if not key:
            return
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = {}
        if API_FILE.exists():
            try:
                data = json.loads(API_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data["gemini_api_key"] = key
        data.setdefault("model_name", "gemini-3.5-flash")
        API_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")

        self._api_key_ready = True
        self._send_now({"type": "key_ok"})
        self.set_state("LISTENING")

    # ── Public API ────────────────────────────────────────────────────────────

    def set_state(self, state: str):
        if state in self.STATES:
            self._state = state
        self.speaking = (state == "SPEAKING")

    def set_level(self, level: float):
        try:
            self._level = max(0.0, min(1.0, float(level)))
        except (TypeError, ValueError):
            pass

    def write_log(self, text: str):
        print(f"[UI] {text}")

    def toggle_mute(self):
        self.muted = not self.muted
        self._tray.refresh()
        self._send_now({"type": "state",
                        "state": "MUTED" if self.muted else self._state,
                        "muted": self.muted, "level": self._level})

    def start_speaking(self):
        self.set_state("SPEAKING")

    def stop_speaking(self):
        if not self.muted:
            self.set_state("LISTENING")

    def wait_for_api_key(self):
        while not self._api_key_ready:
            time.sleep(0.05)

    # ── Tray / background ─────────────────────────────────────────────────────

    def _show_window(self):
        try:
            self._window.show()
            self._window.on_top = True
            self._window.on_top = False
        except Exception:
            pass

    def _hide_window(self):
        """Drop to the tray and keep listening. Full exit if no tray exists."""
        if not self._tray.available:
            self._quit()
            return
        try:
            self._window.hide()
        except Exception:
            pass
        if not self._tray_notified:
            self._tray.notify(
                "Still listening. Right-click the tray icon to show or quit.")
            self._tray_notified = True
        self.write_log("SYS: Minimised to tray — still listening.")

    def _on_closing(self, *_):
        """Window-manager close: hide instead of exit, unless truly quitting."""
        if self._quitting or not self._tray.available:
            return True
        self._hide_window()
        return False

    def _quit(self):
        self._quitting = True
        self._tray.stop()
        os._exit(0)

    def start(self):
        """Blocks until a real quit. Must run on the main thread."""
        self._tray.start()
        webview.start(debug=False)
        # webview.start() only returns on a genuine window teardown.
        os._exit(0)


if _WEB_OK:
    JarvisUI = _WebJarvisUI
else:                                                       # pragma: no cover
    from ui_tk import JarvisUI                               # noqa: F401
