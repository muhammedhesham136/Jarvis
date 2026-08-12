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
    print(f"[UI] WebGL front end unavailable ({_e}) — using fallback renderer.")
    _WEB_OK = False

    # Optional system tray support for the WebView front end. Pillow is required
    # for icon construction and pystray provides the tray integration. If not
    # available, the UI will still work but no tray icon will be created.
    try:
        import pystray
        from PIL import Image, ImageDraw
    except Exception:
        pystray = None
        Image = None
        ImageDraw = None


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

        # Create a system tray icon so the user can hide/restore the WebView.
        try:
            self._create_tray()
        except Exception as e:
            print(f"[UI] tray setup failed: {e}")

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
            self.write_log("SYS: Microphone muted." if self.muted
                           else "SYS: Microphone live.")

        elif kind == "key":
            self._save_key((m.get("value") or "").strip())

        elif kind == "quit":
            # Front-end requests quit: hide UI to tray instead of exiting the
            # whole process so JARVIS keeps running in the background.
            try:
                self._hide_to_tray()
            except Exception as e:
                print(f"[UI] hide to tray failed: {e}")

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
        data.setdefault("model_name", "gemini-2.5-flash")
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

    # ----------------- System tray integration -------------------------------
    def _create_tray(self):
        if pystray is None or Image is None:
            self.write_log("SYS: Install 'pystray' and 'Pillow' to enable tray icon.")
            return
        if getattr(self, "_tray_icon", None) is not None:
            return

        # Build a simple circular icon using PIL
        try:
            img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.ellipse((8, 8, 56, 56), fill=(48, 198, 255, 255))
        except Exception:
            img = None

        def _show_action(icon, item):
            try:
                # Show the WebView window. webview API supports show()/hide().
                if self._window:
                    try:
                        self._window.show()
                    except Exception:
                        pass
            except Exception:
                pass

        def _hide_action(icon, item):
            try:
                self._hide_to_tray()
            except Exception:
                pass

        def _exit_action(icon, item):
            try:
                icon.stop()
            except Exception:
                pass
            os._exit(0)

        menu = pystray.Menu(
            pystray.MenuItem('Show J.A.R.V.I.S', _show_action),
            pystray.MenuItem('Hide J.A.R.V.I.S', _hide_action),
            pystray.MenuItem('Exit J.A.R.V.I.S', _exit_action),
        )

        icon = pystray.Icon('jarvis', img, 'J.A.R.V.I.S', menu)
        self._tray_icon = icon
        t = threading.Thread(target=icon.run, daemon=True)
        t.start()

    def _hide_to_tray(self):
        # Attempt to hide the window rather than destroy it.
        try:
            if self._window:
                try:
                    self._window.hide()
                except Exception:
                    pass
        except Exception:
            pass
        self.write_log("SYS: Web UI hidden (background).")

    def _exit_from_tray(self):
        try:
            if getattr(self, "_tray_icon", None):
                try:
                    self._tray_icon.stop()
                except Exception:
                    pass
            os._exit(0)
        except Exception:
            os._exit(0)

    def toggle_mute(self):
        self.muted = not self.muted
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

    def start(self):
        """Blocks until the window closes. Must run on the main thread.

        Note: do NOT exit the process when the WebView window closes — keep the
        assistant running in the background. The Tk fallback already supports
        hiding to tray; for the WebView front-end the window is frameless, so
        closing it should not terminate the whole program.
        """
        webview.start(debug=False)
        # Don't exit the process so JARVIS continues running when the UI closes.
        self.write_log("SYS: Web UI closed — continuing to run in background.")
        return


if _WEB_OK:
    JarvisUI = _WebJarvisUI
else:                                                       # pragma: no cover
    from ui_tk import JarvisUI                               # noqa: F401
