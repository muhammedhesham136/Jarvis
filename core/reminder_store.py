"""Persistent reminders and to-dos.

One JSON file, safe to call from any thread. A reminder has a due time; a task
has none. Both stay active until the user completes them, so a reminder that
fired while nobody was listening is never lost.
"""

import json
import os
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

STORE_PATH = Path(__file__).resolve().parent.parent / "memory" / "reminders.json"

NAG_EVERY_MIN = 10
NAG_LIMIT     = 3
REPEATS       = ("daily", "weekdays", "weekly", "monthly")

_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _load() -> list[dict]:
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(items: list[dict]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STORE_PATH)


def _dt(value):
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def next_occurrence(due: datetime, repeat: str) -> datetime:
    """The first repeat of `due` that lies in the future."""
    step = {
        "daily":    lambda d: d + timedelta(days=1),
        "weekly":   lambda d: d + timedelta(weeks=1),
        "weekdays": lambda d: d + timedelta(days=3 if d.weekday() == 4
                                            else 2 if d.weekday() == 5 else 1),
        "monthly":  lambda d: (d.replace(year=d.year + 1, month=1) if d.month == 12
                               else _month_forward(d)),
    }[repeat]
    nxt = step(due)
    while nxt <= _now():
        nxt = step(nxt)
    return nxt


def _month_forward(d: datetime) -> datetime:
    for day in (d.day, 28):
        try:
            return d.replace(month=d.month + 1, day=day)
        except ValueError:
            continue
    return d + timedelta(days=30)


# ── Create ───────────────────────────────────────────────────────────────────

def add(message: str, due: datetime | None = None, repeat: str | None = None) -> dict:
    """Add a reminder (with `due`) or a to-do (without)."""
    repeat = repeat if repeat in REPEATS else None
    item = {
        "id":      uuid.uuid4().hex[:8],
        "message": message.strip(),
        "due":     due.isoformat() if due else None,
        "repeat":  repeat if due else None,
        "created": _now().isoformat(),
        "done":    False,
        "fired":   0,
        "next_nag": None,
    }
    with _lock:
        items = _load()
        items.append(item)
        _save(items)
    return item


# ── Query ────────────────────────────────────────────────────────────────────

def active() -> list[dict]:
    with _lock:
        items = [i for i in _load() if not i.get("done")]
    return sorted(items, key=lambda i: (i.get("due") is None, i.get("due") or ""))


def find(query: str) -> dict | None:
    """Match an id, or the best fuzzy hit on the message text."""
    q = (query or "").strip().lower()
    if not q:
        return None
    items = active()
    for i in items:
        if i["id"] == q:
            return i
    words = set(q.split())
    best, score = None, 0
    for i in items:
        msg = i["message"].lower()
        s = 100 if q in msg else len(words & set(msg.split()))
        if s > score:
            best, score = i, s
    return best


def due_now(now: datetime | None = None) -> list[dict]:
    """Reminders whose time has come, or that are unacknowledged and due a nag."""
    now = now or _now()
    out = []
    for i in active():
        due = _dt(i.get("due"))
        if not due:
            continue
        if i["fired"] == 0:
            if due <= now:
                out.append(i)
        elif not i.get("repeat") and i["fired"] <= NAG_LIMIT:
            nag = _dt(i.get("next_nag"))
            if nag and nag <= now:
                out.append(i)
    return out


# ── Update ───────────────────────────────────────────────────────────────────

def mark_fired(item_id: str) -> None:
    """Record a spoken reminder. Repeating ones roll forward; others get nagged."""
    with _lock:
        items = _load()
        for i in items:
            if i["id"] != item_id:
                continue
            due = _dt(i.get("due"))
            if i.get("repeat") and due:
                i["due"], i["fired"], i["next_nag"] = \
                    next_occurrence(due, i["repeat"]).isoformat(), 0, None
            else:
                i["fired"] += 1
                i["next_nag"] = ((_now() + timedelta(minutes=NAG_EVERY_MIN)).isoformat()
                                 if i["fired"] <= NAG_LIMIT else None)
        _save(items)


def complete(item_id: str) -> bool:
    with _lock:
        items = _load()
        hit = False
        for i in items:
            if i["id"] == item_id and not i.get("done"):
                i["done"], hit = True, True
        if hit:
            _save(items)
    return hit


def delete(item_id: str) -> bool:
    with _lock:
        items = _load()
        kept = [i for i in items if i["id"] != item_id]
        if len(kept) != len(items):
            _save(kept)
            return True
    return False


def snooze(item_id: str, minutes: int) -> bool:
    with _lock:
        items = _load()
        hit = False
        for i in items:
            if i["id"] == item_id and not i.get("done") and i.get("due"):
                i["due"] = (_now() + timedelta(minutes=minutes)).isoformat()
                i["fired"], i["next_nag"], hit = 0, None, True
        if hit:
            _save(items)
    return hit


# ── Speech helpers ───────────────────────────────────────────────────────────

def describe(item: dict) -> str:
    due = _dt(item.get("due"))
    if not due:
        return f"{item['message']} (to-do)"
    now = _now()
    if due.date() == now.date():
        when = "today at " + due.strftime("%I:%M %p").lstrip("0")
    elif due.date() == (now + timedelta(days=1)).date():
        when = "tomorrow at " + due.strftime("%I:%M %p").lstrip("0")
    else:
        when = due.strftime("%A %B %d at %I:%M %p").replace(" 0", " ")
    rep = f", repeats {item['repeat']}" if item.get("repeat") else ""
    late = " — OVERDUE" if due < now and not item.get("repeat") else ""
    return f"{item['message']} ({when}{rep}){late}  [id {item['id']}]"
