"""Background assistant: speaks up without being asked.

Runs on its own thread and, every few seconds, checks whether anything needs
saying — a reminder falling due, an unacknowledged reminder that deserves a
nag, or new mail. It never talks over JARVIS or the user: it waits until the
session is idle. Anything unsaid simply stays queued and is tried again.
"""

import json
import threading
import time
from datetime import datetime
from pathlib import Path

from core import reminder_store as store

TICK_SEC        = 15
MAIL_EVERY_SEC  = 180
STATE_PATH      = Path(__file__).resolve().parent.parent / "memory" / "proactive_state.json"

_NOTICE = (
    "[SYSTEM NOTICE — not the user speaking] {body}\n"
    "Tell the user this now, in one or two short sentences, in your usual manner. "
    "Do not call any tool unless the user then asks you to."
)


class Proactive:
    def __init__(self, speak, is_idle, is_online):
        """
        speak(text)   push a turn into the live session
        is_idle()     True when nobody is talking and no tool is running
        is_online()   True when the live session is connected
        """
        self._speak     = speak
        self._is_idle   = is_idle
        self._is_online = is_online
        self._stop      = threading.Event()
        self._last_mail = 0.0
        self._briefed   = False
        self._pending: list[tuple[str, callable]] = []   # (text, on_delivered)
        threading.Thread(target=self._run, daemon=True, name="proactive").start()

    def stop(self):
        self._stop.set()

    # ── Queue ────────────────────────────────────────────────────────────────

    def _enqueue(self, text: str, on_delivered=None):
        self._pending.append((text, on_delivered or (lambda: None)))

    def _flush(self):
        if not self._pending or not self._is_online() or not self._is_idle():
            return
        text, done = self._pending.pop(0)
        self._speak(_NOTICE.format(body=text))
        try:
            done()
        except Exception as e:
            print(f"[proactive] {e}")
        time.sleep(2)   # let the turn start before the next idle check

    # ── Checks ───────────────────────────────────────────────────────────────

    def _check_reminders(self):
        queued = {t for t, _ in self._pending}
        for item in store.due_now():
            nagging = item["fired"] > 0
            text = (f"Reminder{' (again — not yet acknowledged)' if nagging else ''}: "
                    f"{item['message']}. If the user says it's done, mark it complete; "
                    f"if they want more time, snooze it.")
            if text in queued:
                continue
            self._enqueue(text, lambda i=item["id"]: store.mark_fired(i))

    def _check_mail(self):
        if time.time() - self._last_mail < MAIL_EVERY_SEC:
            return
        self._last_mail = time.time()
        try:
            from actions import email_assistant as mail
            res = mail.new_since_last_check()
        except Exception as e:
            print(f"[proactive] mail check failed: {e}")
            return
        if not res or not res["count"]:
            return
        n = res["count"]
        self._enqueue(
            f"{n} new email{'s' if n != 1 else ''} arrived:\n" + "\n".join(res["items"]) +
            "\nMention who they are from and what they are about; offer to read or reply."
        )

    def _briefing(self):
        """Once a day, at the first connection: what is open and overdue."""
        if self._briefed:
            return
        self._briefed = True

        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            state = {}
        today = datetime.now().strftime("%Y-%m-%d")
        if state.get("briefed") == today:
            return

        parts = []
        items = store.active()
        overdue = [i for i in items if i.get("due") and i["due"] < datetime.now().isoformat()
                   and not i.get("repeat")]
        upcoming = [i for i in items if i not in overdue and i.get("due")
                    and i["due"][:10] == today]
        todos = [i for i in items if not i.get("due")]
        if overdue:
            parts.append("Missed while offline: " + "; ".join(i["message"] for i in overdue))
        if upcoming:
            parts.append("Due later today: " + "; ".join(store.describe(i) for i in upcoming))
        if todos:
            parts.append("Open to-dos: " + "; ".join(i["message"] for i in todos[:6]))
        try:
            from actions import email_assistant as mail
            n = mail.unread_count()
            if n:
                parts.append(f"{n} unread emails")
        except Exception:
            pass

        if not parts:
            return
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            STATE_PATH.write_text(json.dumps({"briefed": today}), encoding="utf-8")
        except Exception:
            pass
        self._enqueue("Welcome-back briefing. " + ". ".join(parts) +
                      ". Greet the user briefly and summarise this.")

    # ── Loop ─────────────────────────────────────────────────────────────────

    def _run(self):
        while not self._stop.wait(TICK_SEC):
            try:
                if self._is_online():
                    self._briefing()
                    self._check_reminders()
                    self._check_mail()
                self._flush()
            except Exception as e:
                print(f"[proactive] {e}")
