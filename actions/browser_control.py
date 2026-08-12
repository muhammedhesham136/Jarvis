import asyncio
import threading
import concurrent.futures
import platform
import shutil
import subprocess
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

def _get_default_browser_id() -> str:
    """Returns raw default browser identifier string for current OS."""
    system = platform.system()
    try:
        if system == "Windows":
            import winreg
            key     = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice"
            )
            prog_id = winreg.QueryValueEx(key, "ProgId")[0].lower()
            winreg.CloseKey(key)
            return prog_id

        elif system == "Darwin":
            result = subprocess.run(
                ["defaults", "read",
                 "com.apple.LaunchServices/com.apple.launchservices.secure",
                 "LSHandlers"],
                capture_output=True, text=True, timeout=5
            )
            return result.stdout.lower()

        elif system == "Linux":
            result = subprocess.run(
                ["xdg-settings", "get", "default-web-browser"],
                capture_output=True, text=True, timeout=5
            )
            return result.stdout.lower()

    except Exception:
        pass

    return ""

_BROWSER_BINARIES = {
    "Windows": {
        "opera":   ["opera.exe"],
        "brave":   ["brave.exe"],
        "vivaldi": ["vivaldi.exe"],
        "chrome":  ["chrome.exe"],
        "firefox": ["firefox.exe"],
    },
    "Darwin": {
        "opera":   ["opera"],
        "brave":   ["brave browser", "brave"],
        "vivaldi": ["vivaldi"],
        "chrome":  ["google chrome", "google-chrome"],
        "firefox": ["firefox"],
    },
    "Linux": {
        "opera":   ["opera", "opera-stable"],
        "brave":   ["brave-browser", "brave"],
        "vivaldi": ["vivaldi-stable", "vivaldi"],
        "chrome":  ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium"],
        "firefox": ["firefox"],
    },
}


def _get_opera_executable() -> str | None:
    if platform.system() != "Windows":
        return None
    try:
        import winreg
        candidate_keys = [
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\opera.exe",
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\launcher.exe",
            r"SOFTWARE\Clients\StartMenuInternet\OperaStable\shell\open\command",
            r"SOFTWARE\Clients\StartMenuInternet\OperaGXStable\shell\open\command",
        ]
        for key_path in candidate_keys:
            for hive in [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]:
                try:
                    key  = winreg.OpenKey(hive, key_path)
                    val  = winreg.QueryValue(key, None)
                    winreg.CloseKey(key)
                    exe  = val.strip().strip('"').split('"')[0].split(" --")[0].strip()
                    if exe and Path(exe).exists():
                        print(f"[Browser] 🔍 Opera found via registry: {exe}")
                        return exe
                except Exception:
                    continue
    except Exception:
        pass
    return None


def _find_browser_executable(prog_id: str) -> tuple:
    system  = platform.system()
    os_bins = _BROWSER_BINARIES.get(system, {})

    if any(x in prog_id for x in ["firefox", "mozilla"]):
        return "firefox", None, None

    if "safari" in prog_id:
        return "webkit", None, None

    if "edge" in prog_id:
        return "chromium", None, "msedge"

    if "opera" in prog_id:
        exe = _get_opera_executable()
        if exe:
            return "chromium", exe, None
        for binary in os_bins.get("opera", []):
            path = shutil.which(binary)
            if path:
                return "chromium", path, None

    browser_patterns = {
        "brave":   ["brave"],
        "vivaldi": ["vivaldi"],
        "chrome":  ["chrome"],
    }
    for browser_name, patterns in browser_patterns.items():
        if not any(p in prog_id for p in patterns):
            continue
        binaries = os_bins.get(browser_name, [])
        for binary in binaries:
            path = shutil.which(binary)
            if path:
                print(f"[Browser] 🔍 Found {browser_name} at: {path}")
                return "chromium", path, None

    if "chrome" in prog_id or not prog_id:
        return "chromium", None, "chrome"

    return "chromium", None, None


class _BrowserThread:

    def __init__(self):
        self._loop          = None
        self._thread        = None
        self._ready         = threading.Event()
        self._playwright    = None
        self._browser       = None
        self._context       = None       
        self._incog_context = None       
        self._page          = None       
        self._incog_page    = None       
        self._engine_name   = "chromium" 
        self._exe_path      = None
        self._channel       = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="BrowserThread"
        )
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

    def run(self, coro, timeout: int = 30):
        if not self._loop:
            raise RuntimeError("BrowserThread not started.")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    async def _launch_browser_if_needed(self):
        if self._browser and self._browser.is_connected():
            return

        prog_id = _get_default_browser_id()
        self._engine_name, self._exe_path, self._channel = _find_browser_executable(prog_id)
        
        # Hardcoded to use Brave browser
        self._engine_name = "chromium"
        self._channel = "brave"
        self._exe_path = None
        
        engine = getattr(self._playwright, self._engine_name)

        launch_kwargs = {"headless": False}
        if self._engine_name == "chromium":
            launch_kwargs["args"] = ["--start-maximized"]
        if self._exe_path:
            launch_kwargs["executable_path"] = self._exe_path
        elif self._channel:
            launch_kwargs["channel"] = self._channel

        try:
            self._browser = await engine.launch(**launch_kwargs)
            print(f"[Browser] ✅ Launched ({self._engine_name} / {self._channel})")
        except Exception as e:
            print(f"[Browser] ⚠️ Launch failed ({e}), falling back to default Chromium")
            self._browser = await self._playwright.chromium.launch(
                headless=False,
                args=["--start-maximized"]
            )

    async def _get_page(self, incognito: bool = False):
        await self._launch_browser_if_needed()
        if incognito:
            return await self._get_incognito_page()
        else:
            return await self._get_normal_page()

    async def _get_normal_page(self):
        if self._page is None or self._page.is_closed():
            if self._context is None:
                self._context = await self._browser.new_context(
                    viewport=None,
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                )
            self._page = await self._context.new_page()
        return self._page

    async def _get_incognito_page(self):
        if self._incog_page is None or self._incog_page.is_closed():
            if self._incog_context is None:
                self._incog_context = await self._browser.new_context(
                    viewport=None,
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                )
            self._incog_page = await self._incog_context.new_page()
        return self._incog_page

    async def _open_url_coro(self, url: str, incognito: bool = False):
        page = await self._get_page(incognito=incognito)
        await page.goto(url, wait_until="load")
        await page.bring_to_front()
        return f"Successfully opened {url}"

# Global instance manager
_GLOBAL_BROWSER_THREAD = _BrowserThread()
_GLOBAL_BROWSER_THREAD.start()

def browser_control(parameters: dict, response=None, player=None, session_memory=None) -> str:
    """The master tool function called by main.py."""
    url = (parameters or {}).get("url", "https://google.com").strip()
    incognito = (parameters or {}).get("incognito", False)
    
    if not url.startswith("http"):
        url = "https://" + url
        
    try:
        res = _GLOBAL_BROWSER_THREAD.run(_GLOBAL_BROWSER_THREAD._open_url_coro(url, incognito))
        return res
    except Exception as e:
        return f"Failed to handle web request: {e}"
