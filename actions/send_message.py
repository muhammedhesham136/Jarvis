# actions/send_message.py
# Universal messaging — WhatsApp & Instagram
# Uses visual element detection (pyautogui + screen search) instead of
# hardcoded tab/click sequences — works on any screen resolution.

import time
import pyautogui
from pathlib import Path

try:
    import pyperclip
    _CLIP = True
except Exception:
    _CLIP = False

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.08


def _paste_text(text: str) -> None:
    """
    Enter text through the clipboard.

    pyautogui.write can only type ASCII, so an Arabic name or an emoji comes out
    empty — which is why a contact search would find nothing and no chat opened.
    Pasting supports any language and is faster besides.
    """
    if _CLIP:
        try:
            pyperclip.copy(text)
            time.sleep(0.15)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.25)
            return
        except Exception:
            pass
    pyautogui.write(text, interval=0.03)

def _open_app(app_name: str) -> bool:
    """Opens an app via Windows search."""
    try:
        pyautogui.press("win")
        time.sleep(0.4)
        pyautogui.write(app_name, interval=0.04)
        time.sleep(0.5)
        pyautogui.press("enter")
        time.sleep(2.0)
        return True
    except Exception as e:
        print(f"[SendMessage] Could not open {app_name}: {e}")
        return False


def _ensure_open(app_name: str) -> bool:
    """
    Make the app foreground without opening a second copy.

    If it is already running JARVIS just focuses it and continues from there;
    only when nothing is open does it launch and wait for start-up.
    """
    try:
        from core import desktop_state
        if desktop_state.focus(app_name):
            print(f"[SendMessage] {app_name} already open - continuing")
            time.sleep(0.5)
            return True
    except Exception:
        pass
    return _open_app(app_name)


def _norm(s: str) -> str:
    return " ".join((s or "").replace("\xa0", " ").lower().split())


def _open_chat_in_app(contact: str, app: str = "WhatsApp") -> bool:
    """
    Open a conversation with `contact`, robust to any language and to chats that
    are scrolled out of view.

    First tries a direct UI-Automation click on the chat row — instant when the
    chat is visible. Otherwise it drives the app's own search (Ctrl+N in
    WhatsApp, Ctrl+F elsewhere), pasting the name so Arabic and emoji work, and
    opens the top result. WhatsApp's search matches its stored spelling, which is
    what makes "open Omar's chat" land even when the request came in a different
    script.
    """
    if not _ensure_open(app):
        return False
    time.sleep(0.8)

    # 1) Direct click on a visible chat row via UI Automation (no API cost).
    try:
        import ctypes
        from pywinauto import Desktop
        query = _norm(contact)
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        win = Desktop(backend="uia").window(handle=hwnd)
        seen = 0
        best = None
        for ctrl in win.descendants():
            seen += 1
            if seen > 1500:
                break
            try:
                text = _norm(ctrl.window_text())
            except Exception:
                continue
            if not text:
                continue
            # A chat row's name begins with the contact, then time / preview.
            if text.startswith(query) or query in text:
                try:
                    if ctrl.is_visible() and ctrl.is_enabled():
                        best = ctrl
                        break
                except Exception:
                    continue
        if best is not None:
            best.click_input()
            time.sleep(0.6)
            return True
    except Exception as e:
        print(f"[SendMessage] UIA open failed, using search: {e}")

    # 2) Search fallback — works for any contact, visible or not.
    search_key = ("ctrl", "n") if "whatsapp" in app.lower() else ("ctrl", "f")
    pyautogui.hotkey(*search_key)
    time.sleep(1.2)
    _paste_text(contact)
    time.sleep(1.5)
    pyautogui.press("enter")
    time.sleep(1.0)
    return True


def _search_contact(contact: str, platform: str):
    """
    Searches for a contact inside the messaging app.
    Uses Ctrl+F (universal search shortcut) then types contact name.
    """
    time.sleep(0.5)
    pyautogui.hotkey("ctrl", "f")
    time.sleep(0.4)
    pyautogui.hotkey("ctrl", "a")
    pyautogui.write(contact, interval=0.04)
    time.sleep(0.8)
    pyautogui.press("enter")
    time.sleep(0.6)


def _type_and_send(message: str):
    """Types message and sends it."""
    pyautogui.press("tab")
    time.sleep(0.2)
    pyautogui.hotkey("ctrl", "a")
    pyautogui.write(message, interval=0.03)
    time.sleep(0.2)
    pyautogui.press("enter")
    time.sleep(0.3)


def _send_whatsapp(receiver: str, message: str) -> str:
    """
    Send a WhatsApp message via the Windows desktop app.

    Ctrl+N opens the New-chat search — Ctrl+F would only search *within* the open
    chat, which is why the contact was never found and no chat opened. The name
    and message go in through the clipboard so any language works.
    """
    try:
        if not _open_chat_in_app(receiver, "WhatsApp"):
            return "Could not open WhatsApp."

        _paste_text(message)            # message box is focused once a chat opens
        time.sleep(0.3)
        pyautogui.press("enter")

        return f"Message sent to {receiver} via WhatsApp."

    except Exception as e:
        return f"WhatsApp error: {e}"


def _send_instagram(receiver: str, message: str) -> str:
    """
    Sends an Instagram DM via browser (instagram.com).
    Steps: Open Chrome → Go to instagram.com/direct → Search contact → Send
    """
    try:
        import webbrowser

        webbrowser.open("https://www.instagram.com/direct/new/")
        time.sleep(3.5)

        pyautogui.write(receiver, interval=0.05)
        time.sleep(1.5)

        pyautogui.press("down")
        time.sleep(0.3)
        pyautogui.press("enter")
        time.sleep(0.5)

        for _ in range(3):
            pyautogui.press("tab")
            time.sleep(0.1)
        pyautogui.press("enter")
        time.sleep(1.5)

        pyautogui.write(message, interval=0.04)
        time.sleep(0.2)
        pyautogui.press("enter")

        return f"Message sent to {receiver} via Instagram."

    except Exception as e:
        return f"Instagram error: {e}"

def _send_telegram(receiver: str, message: str) -> str:
    """Sends a Telegram message via Windows desktop app."""
    try:
        if not _ensure_open("Telegram"):
            return "Could not open Telegram."

        time.sleep(1.0)

        pyautogui.hotkey("ctrl", "f")   # Telegram: focuses the global search
        time.sleep(0.6)
        _paste_text(receiver)
        time.sleep(1.5)
        pyautogui.press("enter")        # open the top match
        time.sleep(1.0)

        _paste_text(message)
        time.sleep(0.3)
        pyautogui.press("enter")

        return f"Message sent to {receiver} via Telegram."

    except Exception as e:
        return f"Telegram error: {e}"



def _send_generic(platform: str, receiver: str, message: str) -> str:
    """
    For any other platform not explicitly supported.
    Opens the app, searches for contact, types and sends.
    Works for: Messenger, Discord, Signal, etc.
    """
    try:
        if not _ensure_open(platform):
            return f"Could not open {platform}."

        time.sleep(1.0)
        pyautogui.hotkey("ctrl", "f")
        time.sleep(0.6)
        _paste_text(receiver)
        time.sleep(1.5)
        pyautogui.press("enter")
        time.sleep(1.0)
        _paste_text(message)
        time.sleep(0.3)
        pyautogui.press("enter")

        return f"Message sent to {receiver} via {platform}."

    except Exception as e:
        return f"{platform} error: {e}"

def send_message(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None
) -> str:
    """
    Called from main.py.

    parameters:
        receiver     : Contact name to send to
        message_text : The message content
        platform     : whatsapp | instagram | telegram | <any app name>
                       Default: whatsapp
    """
    params       = parameters or {}
    receiver     = params.get("receiver", "").strip()
    message_text = params.get("message_text", "").strip()
    platform     = params.get("platform", "whatsapp").strip().lower()

    if not receiver:
        return "Please specify who to send the message to, sir."
    if not message_text:
        return "Please specify what message to send, sir."

    print(f"[SendMessage] 📨 {platform} → {receiver}: {message_text[:40]}")
    if player:
        player.write_log(f"[msg] Sending to {receiver} via {platform}...")

    if "whatsapp" in platform or "wp" in platform or "wapp" in platform:
        result = _send_whatsapp(receiver, message_text)

    elif "instagram" in platform or "ig" in platform or "insta" in platform:
        result = _send_instagram(receiver, message_text)

    elif "telegram" in platform or "tg" in platform:
        result = _send_telegram(receiver, message_text)

    else:
        result = _send_generic(platform, receiver, message_text)

    print(f"[SendMessage] ✅ {result}")
    if player:
        player.write_log(f"[msg] {result}")

    return result


def open_chat(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None
) -> str:
    """
    Open a conversation without sending anything.

    parameters:
        contact  : the person or chat to open
        platform : whatsapp | telegram | <app name>   (default: whatsapp)
    """
    params   = parameters or {}
    contact  = (params.get("contact") or params.get("receiver") or "").strip()
    platform = (params.get("platform") or "whatsapp").strip()

    if not contact:
        return "Which chat should I open, sir?"

    print(f"[SendMessage] 📂 open chat: {platform} → {contact}")
    if player:
        player.write_log(f"[chat] Opening {contact} in {platform}...")

    try:
        if _open_chat_in_app(contact, platform):
            return f"Opened the chat with {contact}, sir."
        return f"I couldn't open the chat with {contact}, sir."
    except Exception as e:
        return f"Could not open the chat: {e}"