# actions/outlook_mail.py
# Local mail: reads and answers the mailbox of the Outlook desktop app already
# on this laptop, through Windows (COM). No password, no server, nothing leaves
# the machine except the replies you approve.
#
# Needs classic Outlook (the "New Outlook" toggle must be off — it has no COM)
# and pywin32. Same public functions as the IMAP route in email_assistant.py.

import json
from datetime import datetime, timedelta
from pathlib import Path

STATE_PATH = Path(__file__).resolve().parent.parent / "memory" / "outlook_state.json"

INBOX = 6          # olFolderInbox
MAIL  = 43         # olMail

NOT_AVAILABLE = (
    "I can't reach Outlook on this laptop, sir. It needs the classic Outlook "
    "desktop app (turn off 'New Outlook' in its top-right corner) and "
    "pywin32 installed. Otherwise add an email_address and app password to "
    "config/api_keys.json."
)

_ids: dict[str, str] = {}      # short id shown to the user -> Outlook EntryID


class OutlookUnavailable(Exception):
    pass


def _app():
    try:
        import win32com.client
        return win32com.client.Dispatch("Outlook.Application")
    except Exception as e:
        raise OutlookUnavailable(str(e))


def _inbox():
    return _app().GetNamespace("MAPI").GetDefaultFolder(INBOX)


def is_available() -> bool:
    try:
        _inbox()
        return True
    except Exception:
        return False


# ── Item helpers ─────────────────────────────────────────────────────────────

def _short_id(entry_id: str) -> str:
    for k, v in _ids.items():
        if v == entry_id:
            return k
    short = str(len(_ids) + 1)
    _ids[short] = entry_id
    return short


def _sender_address(item) -> str:
    addr = getattr(item, "SenderEmailAddress", "") or ""
    if getattr(item, "SenderEmailType", "") == "EX":      # Exchange, not SMTP
        try:
            addr = item.Sender.GetExchangeUser().PrimarySmtpAddress
        except Exception:
            pass
    return addr


def _clean_body(text: str) -> str:
    import re
    text = (text or "").replace("\r\n", "\n")
    text = re.split(r"(?m)^(From:\s.+|-{2,}\s*Original Message|On .+wrote:)", text)[0]
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def _line(item, snippet: bool = True) -> str:
    tail = ""
    if snippet:
        tail = " — " + _clean_body(getattr(item, "Body", ""))[:140].replace("\n", " ")
    return (f"[{_short_id(item.EntryID)}] {item.SenderName}: "
            f"{item.Subject or '(no subject)'}{tail}")


def _unread_items(limit: int | None = None):
    items = _inbox().Items
    items.Sort("[ReceivedTime]", True)                    # newest first
    out = []
    for item in items:
        if getattr(item, "Class", MAIL) != MAIL:
            continue
        if getattr(item, "UnRead", False):
            out.append(item)
            if limit and len(out) >= limit:
                break
    return out


def _find(uid: str):
    entry = _ids.get(str(uid))
    if not entry:
        return None
    try:
        return _app().GetNamespace("MAPI").GetItemFromID(entry)
    except Exception:
        return None


# ── State ────────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state), encoding="utf-8")


def _stamp(item) -> str:
    return item.ReceivedTime.strftime("%Y-%m-%dT%H:%M:%S")


def new_since_last_check(limit: int = 5) -> dict | None:
    """Unread mail that arrived since the last call; first call is a baseline."""
    unread = _unread_items(50)
    state  = _load_state()
    last   = state.get("last_received")
    newest = max((_stamp(i) for i in unread), default=last)
    _save_state({"last_received": newest or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")})

    if last is None:
        return {"count": 0, "items": [], "unread": len(unread)}
    fresh = [i for i in unread if _stamp(i) > last]
    fresh.sort(key=_stamp)
    return {"count": len(fresh), "items": [_line(i) for i in fresh[-limit:]],
            "unread": len(unread)}


def unread_count() -> int:
    try:
        return int(_inbox().UnReadItemCount)
    except Exception:
        return len(_unread_items())


# ── Actions ──────────────────────────────────────────────────────────────────

def check(limit: int = 5) -> str:
    total = unread_count()
    if not total:
        return "No unread email, sir."
    items = _unread_items(limit)
    more = f"\n(and {total - len(items)} older unread)" if total > len(items) else ""
    return f"{total} unread. Newest first:\n" + "\n".join(_line(i) for i in items) + more


def read(uid: str) -> str:
    item = _find(uid)
    if not item:
        return f"I couldn't find email {uid}, sir. List your mail again first."
    body = _clean_body(item.Body)
    if len(body) > 2500:
        body = body[:2500] + " … (truncated)"
    return (f"From: {item.SenderName} <{_sender_address(item)}>\n"
            f"Subject: {item.Subject}\nDate: {item.ReceivedTime}\n\n{body}")


def search(query: str, limit: int = 5) -> str:
    q = query.lower().strip()
    items = _inbox().Items
    items.Sort("[ReceivedTime]", True)
    hits, scanned = [], 0
    for item in items:
        if getattr(item, "Class", MAIL) != MAIL:
            continue
        scanned += 1
        if q in f"{item.SenderName} {_sender_address(item)} {item.Subject}".lower():
            hits.append(_line(item, snippet=False))
            if len(hits) >= limit:
                break
        if scanned >= 300:
            break
    return ("Matches:\n" + "\n".join(hits)) if hits else f"Nothing found for '{query}', sir."


def _draft(to: str, subject: str, body: str) -> str:
    return (f"DRAFT — not sent yet.\nTo: {to}\nSubject: {subject}\n\n{body}\n\n"
            "Read this back to the user and ask whether to send it. Only after "
            "a clear yes, call again with confirm=true.")


def reply(uid: str, body: str, confirm: bool) -> str:
    if not body:
        return "What would you like the reply to say, sir?"
    item = _find(uid)
    if not item:
        return f"I couldn't find email {uid}, sir. List your mail again first."
    to = _sender_address(item) or item.SenderName
    subject = item.Subject if str(item.Subject).lower().startswith("re:") else f"Re: {item.Subject}"
    if not confirm:
        return _draft(to, subject, body)
    r = item.Reply()                     # keeps the thread and the quoted history
    r.Body = body + "\n\n" + r.Body
    r.Send()
    return f"Reply sent to {item.SenderName}, sir."


def send(to: str, subject: str, body: str, confirm: bool) -> str:
    if not to or not body:
        return "I need a recipient and a message, sir."
    if "@" not in to:
        return f"'{to}' isn't an email address. Ask the user for the address, or search their mail for it."
    if not confirm:
        return _draft(to, subject or "(no subject)", body)
    mail = _app().CreateItem(0)          # olMailItem
    mail.To, mail.Subject, mail.Body = to, subject or "(no subject)", body
    mail.Send()
    return f"Email sent to {to}, sir."
