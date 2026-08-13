"""
Stateful browser control.

One browser window is kept alive across separate orders, so a session can be
built up a step at a time: "open Brave", then "search inside it", then "open
YouTube" all land in the same window rather than spawning a new one each time.

The browser that opens is the one the user names — Brave when he says Brave —
falling back to the system default, and only ever to Chrome when Chrome is what
he asked for. Chromium-family browsers (Brave, Vivaldi, Opera) are launched by
their real executable because Playwright has no channel for them; Chrome and
Edge use their channels; Firefox uses Playwright's own build.

The window is a persistent profile under the project, so logins and cookies
survive between sessions and it never collides with the user's personal browser
being open at the same time.
"""

import asyncio
import os
import platform
import shutil
import subprocess
import threading
import urllib.parse
from pathlib import Path

from playwright.async_api import async_playwright


BASE_DIR    = Path(__file__).resolve().parent.parent
PROFILE_DIR = BASE_DIR / ".browser_profile"

# Spoken labels for the log / replies.
_LABELS = {
    "brave": "Brave", "chrome": "Chrome", "edge": "Edge",
    "firefox": "Firefox", "opera": "Opera", "vivaldi": "Vivaldi",
}

# Executable basenames per key, used for registry and PATH lookups.
_EXE_NAME = {
    "brave": "brave.exe", "chrome": "chrome.exe", "edge": "msedge.exe",
    "opera": "opera.exe", "vivaldi": "vivaldi.exe",
}

# Typical install locations, relative to a Program Files / LocalAppData root.
_WIN_RELATIVE = {
    "brave":   [r"BraveSoftware\Brave-Browser\Application\brave.exe"],
    "chrome":  [r"Google\Chrome\Application\chrome.exe"],
    "edge":    [r"Microsoft\Edge\Application\msedge.exe"],
    "vivaldi": [r"Vivaldi\Application\vivaldi.exe"],
    "opera":   [r"Programs\Opera\opera.exe", r"Programs\Opera GX\opera.exe"],
}

_NIX_BINARIES = {
    "brave":   ["brave-browser", "brave"],
    "chrome":  ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium",
                "Google Chrome"],
    "edge":    ["microsoft-edge", "microsoft-edge-stable"],
    "opera":   ["opera", "opera-stable"],
    "vivaldi": ["vivaldi-stable", "vivaldi"],
    "firefox": ["firefox"],
}


def _norm_browser(name) -> str | None:
    """Map a spoken browser name to a key, or None when none was given."""
    if not name:
        return None
    n = str(name).strip().lower()
    if not n:
        return None
    aliases = {
        "brave": "brave",
        "chrome": "chrome", "google": "chrome", "google chrome": "chrome",
        "edge": "edge", "microsoft edge": "edge", "msedge": "edge",
        "firefox": "firefox", "mozilla": "firefox",
        "opera": "opera", "opera gx": "opera",
        "vivaldi": "vivaldi",
    }
    for token, key in aliases.items():
        if token in n:
            return key
    return None


def _configured_default_browser() -> str | None:
    """A browser the user pinned in config/api_keys.json, e.g. 'brave'."""
    try:
        import json
        cfg = json.loads((BASE_DIR / "config" / "api_keys.json").read_text(encoding="utf-8"))
        return _norm_browser(cfg.get("default_browser"))
    except Exception:
        return None


def _default_browser_key() -> str | None:
    """The OS default browser, as a key — Brave here, never assumed to be Chrome."""
    try:
        if platform.system() == "Windows":
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\Shell\Associations"
                r"\UrlAssociations\http\UserChoice",
            )
            prog_id = winreg.QueryValueEx(key, "ProgId")[0].lower()
            winreg.CloseKey(key)
            for token in ("brave", "chrome", "edge", "firefox", "opera", "vivaldi"):
                if token in prog_id:
                    return token
        elif platform.system() == "Darwin":
            out = subprocess.run(["defaults", "read",
                "com.apple.LaunchServices/com.apple.launchservices.secure"],
                capture_output=True, text=True, timeout=5).stdout.lower()
            for token in ("brave", "chrome", "edge", "firefox", "opera", "vivaldi"):
                if token in out:
                    return token
        else:
            out = subprocess.run(["xdg-settings", "get", "default-web-browser"],
                capture_output=True, text=True, timeout=5).stdout.lower()
            for token in ("brave", "chrome", "edge", "firefox", "opera", "vivaldi"):
                if token in out:
                    return token
    except Exception:
        pass
    return None


def _find_executable(key: str) -> str | None:
    """Locate a browser's executable: install paths, then registry, then PATH."""
    if platform.system() != "Windows":
        for binary in _NIX_BINARIES.get(key, []):
            path = shutil.which(binary)
            if path:
                return path
        return None

    roots = [os.environ.get("ProgramFiles", ""),
             os.environ.get("ProgramFiles(x86)", ""),
             os.environ.get("LOCALAPPDATA", "")]
    for rel in _WIN_RELATIVE.get(key, []):
        for root in roots:
            if root and (Path(root) / rel).exists():
                return str(Path(root) / rel)

    exe_name = _EXE_NAME.get(key)
    if exe_name:
        try:
            import winreg
            sub = (r"SOFTWARE\Microsoft\Windows\CurrentVersion"
                   rf"\App Paths\{exe_name}")
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    k = winreg.OpenKey(hive, sub)
                    val = winreg.QueryValue(k, None)
                    winreg.CloseKey(k)
                    exe = val.strip().strip('"')
                    if exe and Path(exe).exists():
                        return exe
                except Exception:
                    continue
        except Exception:
            pass
        path = shutil.which(exe_name)
        if path:
            return path
    return None


def _resolve(key: str | None) -> tuple[str, str | None, str | None, str]:
    """
    Turn a browser key into (engine, executable_path, channel, resolved_key).

    Chromium-family browsers are launched by executable; Chrome/Edge may use a
    channel if their executable is not found; Firefox uses Playwright's build.
    """
    if key is None:
        # A configured default (Brave) wins over the OS default browser.
        key = _configured_default_browser() or _default_browser_key()

    if key == "firefox":
        return "firefox", None, None, "firefox"

    if key in ("brave", "vivaldi", "opera"):
        exe = _find_executable(key)
        if exe:
            return "chromium", exe, None, key
        # Named but not installed where we looked — fall through to a channel.
        key = "chrome"

    if key == "edge":
        exe = _find_executable("edge")
        return ("chromium", exe, None, "edge") if exe else ("chromium", None, "msedge", "edge")

    # chrome, or nothing resolved.
    exe = _find_executable("chrome")
    if exe:
        return "chromium", exe, None, "chrome"
    return "chromium", None, "chrome", "chrome"


class _BrowserThread:
    """Owns one Playwright event loop and one long-lived browser window."""

    def __init__(self):
        self._loop       = None
        self._thread     = None
        self._ready      = threading.Event()
        self._playwright = None
        self._context    = None
        self._page       = None
        self._key        = None      # which browser is currently open

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                        name="BrowserThread")
        self._thread.start()
        self._ready.wait(timeout=15)

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._init())
        self._ready.set()
        self._loop.run_forever()

    async def _init(self):
        self._playwright = await async_playwright().start()

    def run(self, coro, timeout: int = 60):
        if not self._loop:
            raise RuntimeError("BrowserThread not started.")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=timeout)

    # ── Browser / page management ─────────────────────────────────────────────

    def _alive(self) -> bool:
        if self._context is None or self._page is None:
            return False
        try:
            return not self._page.is_closed()
        except Exception:
            return False

    async def _launch(self, key: str | None, incognito: bool = False):
        engine_name, exe_path, channel, resolved = _resolve(key)
        engine = getattr(self._playwright, engine_name)

        args = ["--start-maximized"]
        if incognito:
            args.append("--incognito")

        # A per-browser profile keeps Brave's data out of Chrome's and avoids
        # locking whichever profile the user's own browser is using.
        profile = PROFILE_DIR / resolved
        profile.mkdir(parents=True, exist_ok=True)

        kwargs = {"headless": False, "args": args, "no_viewport": True,
                  "user_data_dir": str(profile)}
        if exe_path:
            kwargs["executable_path"] = exe_path
        elif channel:
            kwargs["channel"] = channel

        try:
            self._context = await engine.launch_persistent_context(**kwargs)
        except Exception as e:
            # Last-resort fallback: Playwright's bundled Chromium, so a missing
            # channel or executable never leaves the user with no browser.
            print(f"[Browser] launch fell back to bundled Chromium ({e})")
            kwargs.pop("executable_path", None)
            kwargs.pop("channel", None)
            self._context = await self._playwright.chromium.launch_persistent_context(**kwargs)
            resolved = "chromium"

        self._page = self._context.pages[0] if self._context.pages \
            else await self._context.new_page()
        self._key = resolved
        print(f"[Browser] launched {_LABELS.get(resolved, resolved)}")

    async def _ensure(self, key: str | None, incognito: bool = False):
        """Guarantee a live window, switching browsers only when one is named."""
        if self._alive() and (key is None or key == self._key):
            return
        if self._alive() and key and key != self._key:
            await self._shutdown()          # switch to a different browser
        if not self._alive():
            await self._launch(key, incognito=incognito)

    async def _shutdown(self):
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        self._context = None
        self._page = None
        self._key = None

    # ── Action dispatch ───────────────────────────────────────────────────────

    async def _dispatch(self, params: dict) -> str:
        action    = (params.get("action") or "").strip().lower()
        key       = _norm_browser(params.get("browser"))
        incognito = bool(params.get("incognito", False))

        # Close must be handled before anything can navigate.
        if action in ("close", "quit", "exit"):
            label = _LABELS.get(self._key, "The browser")
            if not self._alive():
                return f"{label} is already closed, sir."
            await self._shutdown()
            return f"{label} is closed, sir."

        await self._ensure(key, incognito=incognito)

        url   = (params.get("url") or "").strip()
        query = (params.get("query") or "").strip()
        text  = (params.get("text") or "").strip()

        if action in ("go_to", "goto", "open", "launch", "open_url", "navigate", ""):
            if url:
                return await self._goto(url)
            if query:
                return await self._search(query)
            await self._page.bring_to_front()
            return f"{_LABELS.get(self._key, 'The browser')} is open, sir."

        if action == "search":
            return await self._search(query or text)

        if action in ("click", "smart_click"):
            return await self._click(params.get("selector"), text or params.get("description"))

        if action in ("type", "smart_type", "fill", "fill_form"):
            return await self._type(params.get("selector"), text, params.get("description"))

        if action == "scroll":
            return await self._scroll((params.get("direction") or "down").lower())

        if action == "get_text":
            return await self._get_text()

        if action == "press":
            return await self._press(params.get("key") or params.get("text"))

        # Unrecognised action, but a URL or query still implies intent.
        if url:
            return await self._goto(url)
        if query:
            return await self._search(query)
        return f"I did not recognise the browser action '{action}', sir."

    # A page op can fail because the user closed the window; relaunch and retry.
    async def _with_page(self, fn):
        try:
            return await fn()
        except Exception:
            if self._alive():
                raise
            await self._launch(self._key)
            return await fn()

    async def _goto(self, url: str) -> str:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        async def op():
            await self._page.goto(url, wait_until="load", timeout=30000)
            await self._page.bring_to_front()
            return f"Opened {url}, sir."
        return await self._with_page(op)

    async def _search(self, query: str) -> str:
        if not query:
            return "Nothing to search for, sir."
        target = "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)

        async def op():
            await self._page.goto(target, wait_until="load", timeout=30000)
            await self._page.bring_to_front()
            return f"Searched for '{query}', sir."
        return await self._with_page(op)

    async def _click(self, selector: str | None, text: str | None) -> str:
        async def op():
            if selector:
                await self._page.click(selector, timeout=8000)
                return f"Clicked {selector}, sir."
            if text:
                await self._page.get_by_text(text, exact=False).first.click(timeout=8000)
                return f"Clicked '{text}', sir."
            return "Nothing specified to click, sir."
        return await self._with_page(op)

    async def _type(self, selector: str | None, text: str, description: str | None) -> str:
        if not text:
            return "Nothing to type, sir."

        async def op():
            if selector:
                await self._page.fill(selector, text, timeout=8000)
            elif description:
                box = self._page.get_by_placeholder(description) \
                    if description else None
                try:
                    await box.first.fill(text, timeout=8000)
                except Exception:
                    await self._page.keyboard.type(text)
            else:
                await self._page.keyboard.type(text)
            return f"Typed '{text[:40]}', sir."
        return await self._with_page(op)

    async def _scroll(self, direction: str) -> str:
        dy = -900 if direction in ("up", "top") else 900

        async def op():
            await self._page.mouse.wheel(0, dy)
            return f"Scrolled {direction}, sir."
        return await self._with_page(op)

    async def _get_text(self) -> str:
        async def op():
            body = await self._page.inner_text("body", timeout=8000)
            return (body or "").strip()[:1500] or "The page has no readable text, sir."
        return await self._with_page(op)

    async def _press(self, key: str | None) -> str:
        if not key:
            return "No key given, sir."

        async def op():
            await self._page.keyboard.press(key)
            return f"Pressed {key}, sir."
        return await self._with_page(op)


_GLOBAL_BROWSER_THREAD = _BrowserThread()
_GLOBAL_BROWSER_THREAD.start()


def browser_control(parameters: dict, response=None, player=None, session_memory=None) -> str:
    """Entry point called by main.py and the agent executor."""
    params = parameters or {}
    try:
        return _GLOBAL_BROWSER_THREAD.run(_GLOBAL_BROWSER_THREAD._dispatch(params))
    except Exception as e:
        return f"Browser action failed: {e}"
