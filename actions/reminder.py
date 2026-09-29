# actions/reminder.py
# Reminders and to-dos, backed by core.reminder_store. The background watcher
# in core.proactive is what actually speaks them when they fall due.

from datetime import datetime, timedelta

from core import reminder_store as store


def _parse_due(p: dict) -> datetime | None:
    minutes = p.get("in_minutes")
    if minutes not in (None, ""):
        return datetime.now().replace(microsecond=0) + timedelta(minutes=int(float(minutes)))

    date_str, time_str = p.get("date"), p.get("time")
    if not date_str and not time_str:
        return None
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")
    if not time_str:
        time_str = "09:00"
    return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")


def reminder(parameters: dict, response=None, player=None, session_memory=None) -> str:
    """
    parameters:
        action    add (default) | add_task | list | complete | delete | snooze
        message   what to be reminded of / the to-do text
        date      YYYY-MM-DD           time      HH:MM (24h)
        in_minutes  relative alternative to date/time
        repeat    daily | weekdays | weekly | monthly
        query     id or words identifying an existing item
        minutes   snooze length
    """
    p      = parameters or {}
    action = str(p.get("action") or "add").lower()

    try:
        if action in ("add", "add_task"):
            message = (p.get("message") or "").strip()
            if not message:
                return "What should I remind you about, sir?"

            due = None if action == "add_task" else _parse_due(p)
            if due and due <= datetime.now():
                return "That time is already in the past, sir."

            item = store.add(message, due, p.get("repeat"))
            if player:
                player.write_log(f"[reminder] {store.describe(item)}")
            if not due:
                return f"Added to your to-do list: {message}."
            return f"Reminder set: {store.describe(item)}."

        if action == "list":
            items = store.active()
            if not items:
                return "You have no open reminders or to-dos, sir."
            return "Open items:\n" + "\n".join(f"- {store.describe(i)}" for i in items)

        target = store.find(p.get("query") or p.get("message") or "")
        if action in ("complete", "delete", "snooze") and not target:
            return "I couldn't find a matching reminder, sir."

        if action == "complete":
            store.complete(target["id"])
            return f"Marked done: {target['message']}."
        if action == "delete":
            store.delete(target["id"])
            return f"Deleted: {target['message']}."
        if action == "snooze":
            minutes = int(float(p.get("minutes") or 10))
            store.snooze(target["id"], minutes)
            return f"I'll remind you again in {minutes} minutes: {target['message']}."

        return f"Unknown reminder action: {action}"

    except ValueError:
        return "I couldn't understand that date or time, sir."
    except Exception as e:
        return f"Something went wrong with the reminder: {str(e)[:80]}"
