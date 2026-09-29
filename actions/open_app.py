# actions/open_app.py
# JARVIS — Cross-Platform App Launcher

import os
import re
import time
import subprocess
import platform
import shutil
from pathlib import Path

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

_APP_ALIASES = {
    "whatsapp":           {"Windows": "WhatsApp",               "Darwin": "WhatsApp",            "Linux": "whatsapp"},
    "chrome":             {"Windows": "chrome",                 "Darwin": "Google Chrome",       "Linux": "google-chrome"},
    "google chrome":      {"Windows": "chrome",                 "Darwin": "Google Chrome",       "Linux": "google-chrome"},
    "firefox":            {"Windows": "firefox",                "Darwin": "Firefox",             "Linux": "firefox"},
    "spotify":            {"Windows": "Spotify",                "Darwin": "Spotify",             "Linux": "spotify"},
    "vscode":             {"Windows": "code",                   "Darwin": "Visual Studio Code",  "Linux": "code"},
    "visual studio code": {"Windows": "code",                   "Darwin": "Visual Studio Code",  "Linux": "code"},
    "discord":            {"Windows": "Discord",                "Darwin": "Discord",             "Linux": "discord"},
    "telegram":           {"Windows": "Telegram",               "Darwin": "Telegram",            "Linux": "telegram"},
    "instagram":          {"Windows": "Instagram",              "Darwin": "Instagram",           "Linux": "instagram"},
    "tiktok":             {"Windows": "TikTok",                 "Darwin": "TikTok",              "Linux": "tiktok"},
    "notepad":            {"Windows": "notepad.exe",            "Darwin": "TextEdit",            "Linux": "gedit"},
    "calculator":         {"Windows": "calc.exe",               "Darwin": "Calculator",          "Linux": "gnome-calculator"},
    "terminal":           {"Windows": "cmd.exe",                "Darwin": "Terminal",            "Linux": "gnome-terminal"},
    "cmd":                {"Windows": "cmd.exe",                "Darwin": "Terminal",            "Linux": "bash"},
    "explorer":           {"Windows": "explorer.exe",           "Darwin": "Finder",              "Linux": "nautilus"},
    "file explorer":      {"Windows": "explorer.exe",           "Darwin": "Finder",              "Linux": "nautilus"},
    "paint":              {"Windows": "mspaint.exe",            "Darwin": "Preview",             "Linux": "gimp"},
    "word":               {"Windows": "winword",                "Darwin": "Microsoft Word",      "Linux": "libreoffice --writer"},
    "excel":              {"Windows": "excel",                  "Darwin": "Microsoft Excel",     "Linux": "libreoffice --calc"},
    "powerpoint":         {"Windows": "powerpnt",               "Darwin": "Microsoft PowerPoint","Linux": "libreoffice --impress"},
    "vlc":                {"Windows": "vlc",                    "Darwin": "VLC",                 "Linux": "vlc"},
    "zoom":               {"Windows": "Zoom",                   "Darwin": "zoom.us",             "Linux": "zoom"},
    "slack":              {"Windows": "Slack",                  "Darwin": "Slack",               "Linux": "slack"},
    "steam":              {"Windows": "steam",                  "Darwin": "Steam",               "Linux": "steam"},
    "task manager":       {"Windows": "taskmgr.exe",            "Darwin": "Activity Monitor",    "Linux": "gnome-system-monitor"},
    "settings":           {"Windows": "ms-settings:",           "Darwin": "System Preferences",  "Linux": "gnome-control-center"},
    "powershell":         {"Windows": "powershell.exe",         "Darwin": "Terminal",            "Linux": "bash"},
    "edge":               {"Windows": "msedge",                 "Darwin": "Microsoft Edge",      "Linux": "microsoft-edge"},
    "brave":              {"Windows": "brave",                  "Darwin": "Brave Browser",       "Linux": "brave-browser"},
    "obsidian":           {"Windows": "Obsidian",               "Darwin": "Obsidian",            "Linux": "obsidian"},
    "notion":             {"Windows": "Notion",                 "Darwin": "Notion",              "Linux": "notion"},
    "blender":            {"Windows": "blender",                "Darwin": "Blender",             "Linux": "blender"},
    "capcut":             {"Windows": "CapCut",                 "Darwin": "CapCut",              "Linux": "capcut"},
    "postman":            {"Windows": "Postman",                "Darwin": "Postman",             "Linux": "postman"},
    "figma":              {"Windows": "Figma",                  "Darwin": "Figma",               "Linux": "figma"},
}


def _remember(app_name: str) -> None:
    """Record the launch so later steps know the app is already open."""
    try:
        from core import desktop_state
        desktop_state.note_opened(app_name)
    except Exception:
        pass


def _normalize(raw: str) -> str:
    system = platform.system()
    key    = raw.lower().strip()
    if key in _APP_ALIASES:
        return _APP_ALIASES[key].get(system, raw)
    for alias_key, os_map in _APP_ALIASES.items():
        if alias_key in key or key in alias_key:
            return os_map.get(system, raw)
    return raw


def _is_running(app_name: str) -> bool:
    if not _PSUTIL:
        return True
    app_lower = app_name.lower().replace(" ", "").replace(".exe", "")
    try:
        for proc in psutil.process_iter(["name"]):
            try:
                proc_name = proc.info["name"].lower().replace(" ", "").replace(".exe", "")
                if app_lower in proc_name or proc_name in app_lower:
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass
    return False


# ── Installed-app discovery (Windows) ────────────────────────────────────────
# Typing into the Start menu is a gamble: the search can rank something else
# first. Instead, look the app up in the shortcuts and Store apps Windows
# itself lists, and start exactly that.

_START_DIRS = (
    Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Microsoft/Windows/Start Menu/Programs",
    Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
)
_startapps_cache: list[tuple[str, str]] | None = None


def _clean(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def best_match(query: str, names: list[str]) -> str | None:
    """Pick the installed-app name that best fits what the user said."""
    q = _clean(query)
    if not q:
        return None
    scored = []
    for name in names:
        n = _clean(name)
        if not n or "uninstall" in n:
            continue
        if n == q:
            score = 100
        elif q in n.split() or f" {q} " in f" {n} ":
            score = 90 - min(len(n), 40) * 0.5      # whole word: "Visual Studio Code"
        elif n.startswith(q):
            score = 60 - min(len(n) - len(q), 30) * 0.5
        elif q in n:
            score = 40
        elif n in q:
            score = 30
        else:
            continue
        scored.append((score, -len(n), name))
    return max(scored)[2] if scored else None


def _shortcuts() -> dict[str, str]:
    found: dict[str, str] = {}
    for base in _START_DIRS:
        if not base.is_dir():
            continue
        for lnk in base.rglob("*.lnk"):
            found.setdefault(lnk.stem, str(lnk))
    return found


def _start_apps() -> list[tuple[str, str]]:
    """(name, AppID) for every app Windows lists, including Store apps."""
    global _startapps_cache
    if _startapps_cache is None:
        _startapps_cache = []
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-StartApps | ForEach-Object { $_.Name + '|' + $_.AppID }"],
                capture_output=True, text=True, timeout=15,
            ).stdout
            for line in out.splitlines():
                name, _, app_id = line.strip().partition("|")
                if name and app_id:
                    _startapps_cache.append((name, app_id))
        except Exception:
            pass
    return _startapps_cache


def _launch_installed_windows(query: str) -> bool:
    lnks = _shortcuts()
    hit = best_match(query, list(lnks))
    if hit:
        try:
            os.startfile(lnks[hit])
            time.sleep(2.0)
            return True
        except Exception as e:
            print(f"[open_app] shortcut launch failed: {e}")

    apps = dict(_start_apps())
    hit = best_match(query, list(apps))
    if hit:
        try:
            subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{apps[hit]}"])
            time.sleep(2.0)
            return True
        except Exception as e:
            print(f"[open_app] Store app launch failed: {e}")
    return False


def _launch_windows(app_name: str) -> bool:
    # Registered command (chrome, code, notepad.exe), URI (ms-settings:), or
    # anything Windows can resolve by name.
    if app_name.endswith(":"):
        try:
            os.startfile(app_name)
            return True
        except Exception:
            pass
    if shutil.which(app_name):
        try:
            subprocess.Popen([shutil.which(app_name)])
            time.sleep(1.5)
            return True
        except Exception:
            pass

    if _launch_installed_windows(app_name):
        return True

    # Last resort: drive the Start menu.
    try:
        import pyautogui
        pyautogui.PAUSE = 0.1
        pyautogui.press("win")
        time.sleep(0.6)
        pyautogui.write(app_name, interval=0.05)
        time.sleep(0.8)
        pyautogui.press("enter")
        time.sleep(3.0)
        return True
    except Exception as e:
        print(f"[open_app] ⚠️ Windows launch failed: {e}")
        return False

def _launch_macos(app_name: str) -> bool:
    try:
        result = subprocess.run(["open", "-a", app_name], capture_output=True, timeout=8)
        if result.returncode == 0:
            time.sleep(1.0)
            return True
    except Exception:
        pass

    try:
        result = subprocess.run(["open", "-a", f"{app_name}.app"], capture_output=True, timeout=8)
        if result.returncode == 0:
            time.sleep(1.0)
            return True
    except Exception:
        pass

    try:
        import pyautogui
        pyautogui.hotkey("command", "space")
        time.sleep(0.6)
        pyautogui.write(app_name, interval=0.05)
        time.sleep(0.8)
        pyautogui.press("enter")
        time.sleep(1.5)
        return True
    except Exception as e:
        print(f"[open_app] ⚠️ macOS Spotlight failed: {e}")
        return False



def _launch_linux(app_name: str) -> bool:
    binary = (
        shutil.which(app_name) or
        shutil.which(app_name.lower()) or
        shutil.which(app_name.lower().replace(" ", "-"))
    )
    if binary:
        try:
            subprocess.Popen([binary], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1.0)
            return True
        except Exception:
            pass

    try:
        subprocess.run(["xdg-open", app_name], capture_output=True, timeout=5)
        return True
    except Exception:
        pass

    try:
        desktop_name = app_name.lower().replace(" ", "-")
        subprocess.run(["gtk-launch", desktop_name], capture_output=True, timeout=5)
        return True
    except Exception:
        pass

    return False


_OS_LAUNCHERS = {
    "Windows": _launch_windows,
    "Darwin":  _launch_macos,
    "Linux":   _launch_linux,
}


def open_app(
    parameters=None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    app_name = (parameters or {}).get("app_name", "").strip()

    if not app_name:
        return "Please specify which application to open, sir."

    system   = platform.system()
    launcher = _OS_LAUNCHERS.get(system)

    if launcher is None:
        return f"Unsupported OS: {system}"

    # Already open? Continue from there rather than launching a second copy.
    try:
        from core import desktop_state
        if desktop_state.focus(app_name):
            print(f"[open_app] {app_name} already open - focused")
            if player:
                player.write_log(f"[open_app] {app_name} already open")
            return f"{app_name} is already open, sir — I've brought it to the front."
    except Exception:
        pass

    normalized = _normalize(app_name)
    print(f"[open_app] 🚀 Launching: {app_name} → {normalized} ({system})")

    if player:
        player.write_log(f"[open_app] {app_name}")

    try:
        success = launcher(normalized)

        if success:
            _remember(app_name)
            return f"Opened {app_name} successfully, sir."

        if normalized != app_name:
            success = launcher(app_name)
            if success:
                _remember(app_name)
                return f"Opened {app_name} successfully, sir."

        return (
            f"I tried to open {app_name}, sir, but couldn't confirm it launched. "
            f"It may still be loading or might not be installed."
        )

    except Exception as e:
        print(f"[open_app] ❌ {e}")
        return f"Failed to open {app_name}, sir: {e}"