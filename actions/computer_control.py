# actions/computer_control.py
# JARVIS — Computer Control
#
# Atomic computer control functions using PyAutoGUI + keyboard + clipboard.
# Used by the agent when no existing action file covers the task.
#
# Capabilities:
#   - Type text anywhere (active window, forms, fields)
#   - Mouse click, double-click, right-click, drag
#   - Keyboard shortcuts and key combinations
#   - Scroll (up/down/left/right)
#   - Window management (minimize, maximize, close, focus)
#   - Clipboard (copy, paste, get content)
#   - Screenshot + locate element on screen
#   - Wait / smart wait for element to appear
#   - Random data generation (name, email, username, password, phone, address)
#   - Hotkey sequences
#   - Find and click image/element on screen

import json
import sys
import time
import random
import string
import subprocess
import platform
from pathlib import Path

try:
    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE    = 0.05
    _PYAUTOGUI = True
except ImportError:
    _PYAUTOGUI = False

try:
    import pyperclip
    _PYPERCLIP = True
except ImportError:
    _PYPERCLIP = False


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


def _load_user_profile() -> dict:
    """Load user profile from long_term.json for form filling."""
    memory_path = BASE_DIR / "memory" / "long_term.json"
    try:
        if memory_path.exists():
            data = json.loads(memory_path.read_text(encoding="utf-8"))
            identity = data.get("identity", {})
            return {
                "name":  identity.get("name",  {}).get("value", ""),
                "age":   identity.get("age",   {}).get("value", ""),
                "city":  identity.get("city",  {}).get("value", ""),
                "email": identity.get("email", {}).get("value", ""),
            }
    except Exception:
        pass
    return {}


def _ensure_pyautogui():
    if not _PYAUTOGUI:
        raise RuntimeError(
            "PyAutoGUI not installed. Run: pip install pyautogui"
        )


_FIRST_NAMES = [
    "Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Drew", "Quinn",
    "Avery", "Blake", "Cameron", "Dakota", "Emerson", "Finley", "Harper"
]
_LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Wilson", "Moore", "Taylor", "Anderson", "Thomas", "Jackson"
]
_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "proton.me", "mail.com"]


def generate_random_data(data_type: str) -> str:
    """
    Generates random realistic data for form filling.

    Types: name | first_name | last_name | email | username |
           password | phone | birthday | address | zip_code
    """
    dt = data_type.lower().strip()

    if dt == "first_name":
        return random.choice(_FIRST_NAMES)

    elif dt == "last_name":
        return random.choice(_LAST_NAMES)

    elif dt == "name":
        return f"{random.choice(_FIRST_NAMES)} {random.choice(_LAST_NAMES)}"

    elif dt == "email":
        first = random.choice(_FIRST_NAMES).lower()
        last  = random.choice(_LAST_NAMES).lower()
        num   = random.randint(10, 999)
        return f"{first}.{last}{num}@{random.choice(_DOMAINS)}"

    elif dt == "username":
        first = random.choice(_FIRST_NAMES).lower()
        num   = random.randint(100, 9999)
        return f"{first}{num}"

    elif dt == "password":
        chars = string.ascii_letters + string.digits + "!@#$%"
        pwd   = (
            random.choice(string.ascii_uppercase) +
            random.choice(string.digits) +
            random.choice("!@#$%") +
            "".join(random.choices(chars, k=9))
        )
        return "".join(random.sample(pwd, len(pwd)))

    elif dt == "phone":
        return f"+1{random.randint(200,999)}{random.randint(1000000,9999999)}"

    elif dt == "birthday":
        year  = random.randint(1980, 2000)
        month = random.randint(1, 12)
        day   = random.randint(1, 28)
        return f"{month:02d}/{day:02d}/{year}"

    elif dt == "address":
        num    = random.randint(100, 9999)
        street = random.choice(["Main St", "Oak Ave", "Park Blvd", "Elm St", "Cedar Ln"])
        return f"{num} {street}"

    elif dt == "zip_code":
        return str(random.randint(10000, 99999))

    elif dt == "city":
        return random.choice(["New York", "Los Angeles", "Chicago", "Houston", "Phoenix"])

    return f"random_{data_type}_{random.randint(1000,9999)}"


def _type_text(text: str, interval: float = 0.03) -> str:
    """Types text at the current cursor position."""
    _ensure_pyautogui()
    time.sleep(0.3)
    pyautogui.typewrite(text, interval=interval)
    return f"Typed: {text[:50]}{'...' if len(text) > 50 else ''}"


def _click(x: int = None, y: int = None, button: str = "left",
           clicks: int = 1, image: str = None) -> str:
    """
    Clicks at coordinates or on a screen image.
    If image path given, locates it on screen and clicks.
    """
    _ensure_pyautogui()

    if image:
        try:
            loc = pyautogui.locateCenterOnScreen(image, confidence=0.8)
            if loc:
                pyautogui.click(loc.x, loc.y, button=button, clicks=clicks)
                return f"Clicked image: {image}"
            return f"Image not found on screen: {image}"
        except Exception as e:
            return f"Image click failed: {e}"

    if x is not None and y is not None:
        pyautogui.click(x, y, button=button, clicks=clicks)
        return f"Clicked ({x}, {y}) with {button} button"

    pyautogui.click(button=button, clicks=clicks)
    return f"Clicked at current position"


def _hotkey(*keys) -> str:
    """Presses a key combination. E.g. hotkey('ctrl', 'c')"""
    _ensure_pyautogui()
    pyautogui.hotkey(*keys)
    return f"Hotkey: {'+'.join(keys)}"


def _press(key: str) -> str:
    """Presses a single key."""
    _ensure_pyautogui()
    pyautogui.press(key)
    return f"Pressed: {key}"


def _scroll(direction: str = "down", amount: int = 3) -> str:
    """Scrolls in the specified direction."""
    _ensure_pyautogui()
    clicks = amount if direction in ("up", "right") else -amount
    if direction in ("up", "down"):
        pyautogui.scroll(clicks)
    else:
        pyautogui.hscroll(clicks)
    return f"Scrolled {direction} {amount} times"


def _move_mouse(x: int, y: int, duration: float = 0.3) -> str:
    """Moves mouse to coordinates."""
    _ensure_pyautogui()
    pyautogui.moveTo(x, y, duration=duration)
    return f"Mouse moved to ({x}, {y})"


def _drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.5) -> str:
    """Drags from (x1,y1) to (x2,y2)."""
    _ensure_pyautogui()
    pyautogui.drag(x1 - pyautogui.position()[0], y1 - pyautogui.position()[1])
    pyautogui.dragTo(x2, y2, duration=duration)
    return f"Dragged from ({x1},{y1}) to ({x2},{y2})"


def _clipboard_copy() -> str:
    """Gets current clipboard content."""
    if _PYPERCLIP:
        return pyperclip.paste()
    _hotkey("ctrl", "c")
    time.sleep(0.2)
    return "Copied to clipboard"


def _clipboard_set(text: str) -> str:
    """Sets clipboard content and pastes it."""
    if _PYPERCLIP:
        pyperclip.copy(text)
        time.sleep(0.1)
        _hotkey("ctrl", "v")
        return f"Pasted: {text[:50]}"
    return "pyperclip not available"


def _screenshot(save_path: str = None) -> str:
    """Takes a screenshot."""
    _ensure_pyautogui()
    if not save_path:
        save_path = str(Path.home() / "Desktop" / "screenshot.png")
    img = pyautogui.screenshot()
    img.save(save_path)
    return f"Screenshot saved: {save_path}"


def _wait(seconds: float) -> str:
    """Waits for specified seconds."""
    time.sleep(seconds)
    return f"Waited {seconds}s"


def _wait_for_image(image_path: str, timeout: int = 10) -> str:
    """Waits until an image appears on screen (up to timeout seconds)."""
    _ensure_pyautogui()
    start = time.time()
    while time.time() - start < timeout:
        try:
            loc = pyautogui.locateCenterOnScreen(image_path, confidence=0.8)
            if loc:
                return f"Image found at ({loc.x}, {loc.y})"
        except Exception:
            pass
        time.sleep(0.5)
    return f"Image not found within {timeout}s: {image_path}"


def _get_screen_size() -> str:
    """Returns current screen resolution."""
    _ensure_pyautogui()
    w, h = pyautogui.size()
    return f"{w}x{h}"


def _focus_window(title: str) -> str:
    """Brings a window to focus by title (Windows)."""
    if platform.system() == "Windows":
        try:
            script = f'(New-Object -ComObject WScript.Shell).AppActivate("{title}")'
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True, timeout=5
            )
            time.sleep(0.3)
            return f"Focused window: {title}"
        except Exception as e:
            return f"Could not focus window: {e}"
    return "Window focus only supported on Windows"


def _select_all() -> str:
    return _hotkey("ctrl", "a")


def _clear_field() -> str:
    """Selects all and deletes — clears an input field."""
    _hotkey("ctrl", "a")
    time.sleep(0.1)
    _press("delete")
    return "Field cleared"


def _smart_type(text: str, clear_first: bool = True, description: str = "") -> str:
    """
    Type text into a field.

    If a field is named it is focused first — otherwise the text lands wherever
    focus happens to be, which is why a blind search never entered anything.
    Any non-ASCII text (Arabic, emoji) goes through the clipboard, since
    pyautogui.typewrite can only produce ASCII and silently drops the rest.
    """
    _ensure_pyautogui()

    if description:
        try:
            ok, _ = _click_by_text(description)
            if ok:
                time.sleep(0.25)
        except Exception:
            pass

    if clear_first:
        _clear_field()
        time.sleep(0.1)

    non_ascii = any(ord(ch) > 127 for ch in text)
    if _PYPERCLIP and (non_ascii or len(text) > 20):
        pyperclip.copy(text)
        time.sleep(0.12)
        pyautogui.hotkey("ctrl", "v")
        return f"Typed: {text[:50]}"

    pyautogui.typewrite(text, interval=0.04)
    return f"Typed: {text[:50]}"


def _analyze_screen_for_element(description: str) -> tuple[int, int] | None:
    """
    Takes a screenshot and asks Gemini to find the coordinates
    of a described element on screen. Returns (x, y) or None.
    """
    try:
        from core import genai_compat as genai
        import io

        # A single read of the config — the module-level json is used throughout;
        # a second `import json` here would shadow it and break the line above.
        cfg = json.loads(Path(API_CONFIG_PATH).read_text(encoding="utf-8"))
        api_key = cfg.get("gemini_api_key", "")

        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(cfg.get("model_name", "gemini-3.5-flash"))

        _ensure_pyautogui()
        w, h  = pyautogui.size()
        img   = pyautogui.screenshot()
        buf   = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)

        prompt = (
            "This is a screenshot of a computer screen. "
            f"Find the element: '{description}'. "
            "Respond with ONLY two integers 'x,y' for the CENTRE of that element, "
            "each on a 0-1000 scale (x from the left edge, y from the top edge). "
            "If it is not visible, respond with exactly: NOT_FOUND"
        )

        response = model.generate_content([
            {"mime_type": "image/png", "data": buf.getvalue()},
            prompt
        ])

        text = response.text.strip()
        if "NOT_FOUND" in text:
            return None

        import re
        match = re.search(r"(\d+)\s*,\s*(\d+)", text)
        if match:
            # The model is asked for a point on a 0-1000 grid; scale to pixels.
            nx, ny = int(match.group(1)), int(match.group(2))
            if nx <= 1000 and ny <= 1000:
                return int(nx / 1000 * w), int(ny / 1000 * h)
            return nx, ny

    except Exception as e:
        print(f"[ComputerControl] ⚠️ Screen analysis failed: {e}")

    return None


def _norm(s: str) -> str:
    """Lower-case, collapse whitespace, drop the non-breaking spaces UIA emits."""
    return " ".join((s or "").replace("\xa0", " ").lower().split())


def _match_score(query: str, text: str) -> int:
    """How well a control's visible text answers 'click on <query>'. 0 = no match."""
    if not text:
        return 0
    if text == query:
        return 100
    if text.startswith(query) or query.startswith(text):
        return 85
    if query in text:
        return 70
    q_words = query.split()
    if q_words and all(w in text for w in q_words):
        return 50
    return 0


def _click_by_text(description: str, double: bool = False) -> tuple[bool, str | None]:
    """
    Click a control by its visible name via UI Automation.

    This reads the real accessibility tree, so it lands on the actual control —
    a chat, a button, a menu item — with no screenshot and no API call. Falls
    back to the vision finder only when nothing here matches.
    """
    try:
        import ctypes
        from pywinauto import Desktop
    except Exception:
        return (False, None)

    query = _norm(description)
    if not query:
        return (False, None)

    def scan(root, budget=1200):
        # budget caps how many controls we examine — a window with thousands of
        # descendants and no match must not stall the assistant.
        found = []
        seen = 0
        try:
            for ctrl in root.descendants():
                seen += 1
                if seen > budget:
                    break
                try:
                    text = _norm(ctrl.window_text())
                except Exception:
                    continue
                score = _match_score(query, text)
                if score <= 0:
                    continue
                try:
                    if not (ctrl.is_visible() and ctrl.is_enabled()):
                        continue
                except Exception:
                    pass
                found.append((score, len(text), ctrl, text))
                if len(found) > 60:
                    break
        except Exception:
            pass
        return found

    candidates = []
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        candidates = scan(Desktop(backend="uia").window(handle=hwnd))
    except Exception:
        candidates = []

    # Nothing in the focused window — widen to other titled top-level windows,
    # newest first, but only a few and each on a tight budget.
    if not candidates:
        try:
            checked = 0
            for win in Desktop(backend="uia").windows(visible_only=True):
                try:
                    if not (win.window_text() or "").strip():
                        continue
                except Exception:
                    continue
                candidates = scan(win, budget=800)
                checked += 1
                if candidates or checked >= 6:
                    break
        except Exception:
            pass

    if not candidates:
        return (False, None)

    # Best score wins; among equals, the shortest label is the tightest match.
    candidates.sort(key=lambda c: (-c[0], c[1]))
    _, _, ctrl, text = candidates[0]

    try:
        ctrl.set_focus()
    except Exception:
        pass
    try:
        ctrl.double_click_input() if double else ctrl.click_input()
        return (True, text)
    except Exception:
        try:
            r = ctrl.rectangle()
            _click(x=(r.left + r.right) // 2, y=(r.top + r.bottom) // 2,
                   clicks=2 if double else 1)
            return (True, text)
        except Exception:
            return (False, None)

def computer_control(
    parameters:     dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """
    Universal computer control action.

    Actions:
      type          : Type text at current cursor position
      smart_type    : Clear field + type (clipboard-based for long text)
      click         : Click at coordinates or on image
      double_click  : Double click
      right_click   : Right click
      hotkey        : Key combination (e.g. ctrl+c)
      press         : Single key press
      scroll        : Scroll up/down/left/right
      move          : Move mouse to coordinates
      drag          : Drag from one point to another
      copy          : Get clipboard content
      paste         : Set and paste clipboard content
      screenshot    : Take a screenshot
      wait          : Wait N seconds
      wait_image    : Wait for image to appear on screen
      clear_field   : Select all + delete in current field
      focus_window  : Bring window to foreground
      screen_find   : AI-powered element finder — returns coordinates
      screen_click  : AI-powered element finder + click
      random_data   : Generate random data for forms
      user_data     : Get user's real data from memory
    """
    action = (parameters or {}).get("action", "").lower().strip()

    if not action:
        return "Please specify an action for computer_control, sir."

    if player:
        player.write_log(f"[Computer] {action}")

    print(f"[ComputerControl] ▶️ Action: {action}  Params: {parameters}")

    try:
        if action == "type":
            text = parameters.get("text", "")
            return _type_text(text)

        elif action == "smart_type":
            text        = parameters.get("text", "")
            clear_first = parameters.get("clear_first", True)
            description = parameters.get("description", "")
            return _smart_type(text, clear_first=clear_first, description=description)
        
        elif action in ("click", "left_click"):
            # "click on the Sara chat" — a description with no coordinates means
            # find it by name and click it, rather than clicking where the mouse is.
            desc = parameters.get("description") or parameters.get("text")
            if desc and parameters.get("x") is None and parameters.get("y") is None \
                    and not parameters.get("image"):
                ok, label = _click_by_text(desc)
                if ok:
                    return f"Clicked '{label}', sir."
                coords = _analyze_screen_for_element(desc)
                if coords:
                    time.sleep(0.2)
                    _click(x=coords[0], y=coords[1])
                    return f"Clicked {desc}, sir."
                return f"I couldn't find '{desc}' on the screen, sir."
            return _click(
                x=parameters.get("x"),
                y=parameters.get("y"),
                button="left",
                clicks=1,
                image=parameters.get("image")
            )

        elif action == "double_click":
            return _click(
                x=parameters.get("x"),
                y=parameters.get("y"),
                button="left",
                clicks=2,
                image=parameters.get("image")
            )

        elif action == "right_click":
            return _click(
                x=parameters.get("x"),
                y=parameters.get("y"),
                button="right",
                clicks=1
            )

        elif action == "move":
            return _move_mouse(
                x=int(parameters.get("x", 0)),
                y=int(parameters.get("y", 0)),
                duration=float(parameters.get("duration", 0.3))
            )

        elif action == "drag":
            return _drag(
                x1=int(parameters.get("x1", 0)),
                y1=int(parameters.get("y1", 0)),
                x2=int(parameters.get("x2", 0)),
                y2=int(parameters.get("y2", 0))
            )

        elif action == "hotkey":
            keys = parameters.get("keys", "")
            if isinstance(keys, str):
                keys = [k.strip() for k in keys.split("+")]
            return _hotkey(*keys)

        elif action == "press":
            return _press(parameters.get("key", "enter"))

        elif action == "scroll":
            return _scroll(
                direction=parameters.get("direction", "down"),
                amount=int(parameters.get("amount", 3))
            )

        elif action == "copy":
            return _clipboard_copy()

        elif action == "paste":
            return _clipboard_set(parameters.get("text", ""))

        elif action == "screenshot":
            return _screenshot(parameters.get("path"))

        elif action == "wait":
            return _wait(float(parameters.get("seconds", 1.0)))

        elif action == "wait_image":
            return _wait_for_image(
                parameters.get("image", ""),
                timeout=int(parameters.get("timeout", 10))
            )

        elif action == "clear_field":
            return _clear_field()

        elif action == "focus_window":
            return _focus_window(parameters.get("title", ""))

        elif action == "screen_size":
            return _get_screen_size()

        elif action == "screen_find":
            description = parameters.get("description", "") or parameters.get("text", "")
            coords = _analyze_screen_for_element(description)
            if coords:
                return f"{coords[0]},{coords[1]}"
            return "NOT_FOUND"

        elif action in ("screen_click", "smart_click"):
            description = parameters.get("description", "") or parameters.get("text", "")
            if not description:
                return "Tell me what to click, sir."
            # UI Automation first — precise, instant, no API cost.
            ok, label = _click_by_text(description)
            if ok:
                return f"Clicked '{label}', sir."
            # Fall back to the vision finder.
            coords = _analyze_screen_for_element(description)
            if coords:
                time.sleep(0.2)
                _click(x=coords[0], y=coords[1])
                return f"Clicked {description}, sir."
            return f"I couldn't find '{description}' on the screen, sir."

        elif action == "random_data":
            data_type = parameters.get("type", "name")
            result    = generate_random_data(data_type)
            print(f"[ComputerControl] 🎲 Random {data_type}: {result}")
            return result

        elif action == "user_data":
            field   = parameters.get("field", "name")
            profile = _load_user_profile()
            value   = profile.get(field, "")
            if not value:
                value = generate_random_data(field)
                print(f"[ComputerControl] ⚠️ No user {field} in memory, using random: {value}")
            return value

        else:
            return f"Unknown computer_control action: '{action}'"

    except Exception as e:
        print(f"[ComputerControl] ❌ Error: {e}")
        return f"computer_control failed: {e}"