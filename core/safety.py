"""Safety layer for everything JARVIS runs on this machine.

Two jobs:

* Before a generated program or shell command runs, look for actions that are
  hard to undo or leave the machine (deleting, installing, sending, changing
  system settings). If there are any, ask the user — with a real Windows dialog,
  not a question the model could answer for itself.
* Record what was run and how it ended, so "what did you just do?" has an answer.

Turn the dialog off with  "confirm_risky_actions": false  in config/api_keys.json.
"""

import json
import re
import sys
import threading
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE / "config" / "api_keys.json"
LOG_PATH    = BASE / "memory" / "activity.jsonl"

CONFIRM_TIMEOUT = 90     # seconds to wait for a click before treating it as "no"

# (reason shown to the user, pattern). Deliberately about *effects*, so it
# matches both Python source and shell text.
_RISKS = [
    ("delete files or folders", re.compile(
        r"(?i)shutil\.rmtree|os\.(?:remove|unlink|rmdir|removedirs)\b|\.unlink\(|\.rmdir\(|"
        r"send2trash|\b(?:del|erase|rd|rmdir)\s+/|\brm\s+-|remove-item\b")),
    ("install or remove software", re.compile(
        r"(?i)pip\s+install|pip\s+uninstall|\bwinget\s+(?:install|uninstall|upgrade)|"
        r"\bchoco\s+(?:install|uninstall)|msiexec|\bscoop\s+install|npm\s+(?:i|install)\s+-g")),
    ("send data out (email or network upload)", re.compile(
        r"(?i)smtplib|\.sendmail\(|send_message\(|requests\.(?:post|put|patch|delete)\b|"
        r"urlopen\([^)]*data\s*=|invoke-restmethod\s+.*-method\s+(?:post|put|delete)|curl\s+.*-X\s+(?:POST|PUT|DELETE)")),
    ("change system settings or the registry", re.compile(
        r"(?i)winreg|\breg\s+(?:add|delete|import)\b|set-itemproperty\s+.*hkl?[mu]|\bnetsh\b|"
        r"schtasks\s+/(?:create|delete|change)|set-executionpolicy|\bsc\s+(?:config|delete|stop)\b|"
        r"new-service|disable-\w+|\bbcdedit\b")),
    ("shut down, restart or kill processes", re.compile(
        r"(?i)\bshutdown\b|restart-computer|stop-computer|\btaskkill\b|stop-process|"
        r"\.terminate\(|\.kill\(|os\.kill\(")),
    ("run downloaded or encoded code", re.compile(
        r"(?i)\bexec\(|\beval\(|iex\b|invoke-expression|-enc(?:odedcommand)?\b|"
        r"base64\.b64decode|downloadstring|frombase64string")),
    ("move or rename files", re.compile(
        r"(?i)shutil\.move|os\.rename|os\.replace|\bmove-item\b|\bmove\s+/y|robocopy\s+.*(?:/mir|/move)")),
]


def risks(text: str) -> list[str]:
    """Human-readable reasons this program or command needs approval."""
    return [why for why, pat in _RISKS if pat.search(text or "")]


def _enabled() -> bool:
    try:
        return bool(json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                    .get("confirm_risky_actions", True))
    except Exception:
        return True


def _dialog(title: str, message: str) -> bool:
    """A native Yes/No box, always on top. Anything but a clear Yes is a No."""
    if sys.platform != "win32":
        return False
    import ctypes
    MB_YESNO, MB_ICONWARNING, MB_TOPMOST, MB_SETFOREGROUND = 0x4, 0x30, 0x40000, 0x10000
    IDYES = 6
    result = {"answer": False}

    def ask():
        result["answer"] = ctypes.windll.user32.MessageBoxW(
            0, message, title,
            MB_YESNO | MB_ICONWARNING | MB_TOPMOST | MB_SETFOREGROUND) == IDYES

    t = threading.Thread(target=ask, daemon=True)
    t.start()
    t.join(CONFIRM_TIMEOUT)
    return result["answer"] if not t.is_alive() else False


def approve(kind: str, what: str, reasons: list[str], hard: bool = False) -> bool:
    """
    True when the action may go ahead. No reasons -> no question asked.
    `hard` marks things that can wreck the machine (drive wipes) and always ask.
    """
    if not reasons:
        return True
    if not hard and not _enabled():
        return True
    body = (f"JARVIS is about to run {kind} that will:\n\n  • " +
            "\n  • ".join(reasons) +
            f"\n\n{what[:400]}\n\nAllow it?")
    return _dialog("JARVIS — please confirm", body)


def log(tool: str, what: str, outcome: str, reasons: list[str] | None = None,
        approved: bool | None = None) -> None:
    """Append one line to memory/activity.jsonl. Never raises."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "tool": tool,
            "what": what[:600],
            "risks": reasons or [],
            "approved": approved,
            "outcome": (outcome or "")[:300],
        }
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def recent(n: int = 10) -> list[dict]:
    try:
        lines = LOG_PATH.read_text(encoding="utf-8").splitlines()[-n:]
        return [json.loads(l) for l in lines]
    except Exception:
        return []
