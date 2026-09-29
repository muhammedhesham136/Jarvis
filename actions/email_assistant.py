# actions/email_assistant.py
# Read, search, summarise and reply to email over IMAP/SMTP.
#
# Works with any provider that offers an app password (Gmail, Outlook, Yahoo,
# iCloud...). Credentials live in config/api_keys.json:
#   "email_address":  "you@gmail.com"
#   "email_password": "<app password>"
#   optional "imap_host", "smtp_host" for providers not listed below.
#
# Nothing is ever sent without confirm=true, so JARVIS must read the draft back
# and get a yes first.

import email
import imaplib
import json
import re
import smtplib
import ssl
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parseaddr, formatdate, make_msgid
from html import unescape
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"
STATE_PATH  = Path(__file__).resolve().parent.parent / "memory" / "email_state.json"

_HOSTS = {
    "gmail.com":      ("imap.gmail.com",          "smtp.gmail.com"),
    "googlemail.com": ("imap.gmail.com",          "smtp.gmail.com"),
    "outlook.com":    ("outlook.office365.com",   "smtp.office365.com"),
    "hotmail.com":    ("outlook.office365.com",   "smtp.office365.com"),
    "live.com":       ("outlook.office365.com",   "smtp.office365.com"),
    "yahoo.com":      ("imap.mail.yahoo.com",     "smtp.mail.yahoo.com"),
    "icloud.com":     ("imap.mail.me.com",        "smtp.mail.me.com"),
    "me.com":         ("imap.mail.me.com",        "smtp.mail.me.com"),
}

SETUP_HELP = (
    "Email isn't set up yet, sir. Add email_address and email_password (an app "
    "password, not your normal one) to config/api_keys.json. For Gmail, turn on "
    "2-step verification and create an app password at myaccount.google.com/apppasswords."
)


# ── Config / connections ─────────────────────────────────────────────────────

def _cfg() -> dict | None:
    try:
        c = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    addr, pw = c.get("email_address"), c.get("email_password")
    if not addr or not pw:
        return None
    imap_h, smtp_h = _HOSTS.get(addr.split("@")[-1].lower(), (None, None))
    return {
        "address":   addr,
        "password":  pw.replace(" ", ""),
        "imap_host": c.get("imap_host") or imap_h,
        "smtp_host": c.get("smtp_host") or smtp_h,
    }


def is_configured() -> bool:
    c = _cfg()
    return bool(c and c["imap_host"] and c["smtp_host"])


def _imap(cfg: dict) -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(cfg["imap_host"], timeout=20)
    conn.login(cfg["address"], cfg["password"])
    conn.select("INBOX", readonly=True)
    return conn


# ── Parsing ──────────────────────────────────────────────────────────────────

def _decode(value) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return str(value)


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", html)
    return unescape(re.sub(r"<[^>]+>", " ", html))


def _body(msg: email.message.Message) -> str:
    plain, html = "", ""
    for part in msg.walk():
        if part.is_multipart() or part.get_filename():
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            text = part.get_payload(decode=True).decode(
                part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if ctype == "text/plain":
            plain += text
        else:
            html += text
    text = plain.strip() or _strip_html(html)
    # Drop quoted history so a reply chain reads as just the newest message.
    text = re.split(r"(?m)^(On .+wrote:|-{2,}\s*Original Message|From:\s.+$)", text)[0]
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def _summary(uid: str, msg: email.message.Message) -> dict:
    name, addr = parseaddr(_decode(msg.get("From")))
    return {
        "uid":     uid,
        "from":    name or addr,
        "address": addr,
        "subject": _decode(msg.get("Subject")) or "(no subject)",
        "date":    msg.get("Date", ""),
    }


def _fetch(conn, uid: str) -> email.message.Message | None:
    # BODY.PEEK keeps the message unread — looking is not the same as reading.
    typ, data = conn.uid("FETCH", uid, "(BODY.PEEK[])")
    if typ != "OK" or not data or not isinstance(data[0], tuple):
        return None
    return email.message_from_bytes(data[0][1])


def _line(s: dict, snippet: str = "") -> str:
    tail = f" — {snippet}" if snippet else ""
    return f"[{s['uid']}] {s['from']}: {s['subject']}{tail}"


# ── State (what has already been announced) ──────────────────────────────────

def _state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state), encoding="utf-8")


def new_since_last_check(limit: int = 5) -> dict | None:
    """
    For the background watcher: unread mail that arrived since the last call.
    The first call only sets a baseline, so a full inbox is not announced.
    """
    cfg = _cfg()
    if not cfg or not is_configured():
        return None
    conn = _imap(cfg)
    try:
        typ, data = conn.uid("SEARCH", None, "UNSEEN")
        uids = [int(u) for u in (data[0] or b"").split()] if typ == "OK" else []
        state = _state()
        last  = state.get("last_uid")
        state["last_uid"] = max(uids + ([last] if last else [])) if (uids or last) else 0
        _save_state(state)

        if last is None:
            return {"count": 0, "items": [], "unread": len(uids)}
        fresh = [u for u in uids if u > last]
        items = []
        for u in fresh[-limit:]:
            msg = _fetch(conn, str(u))
            if msg:
                items.append(_line(_summary(str(u), msg), _body(msg)[:120].replace("\n", " ")))
        return {"count": len(fresh), "items": items, "unread": len(uids)}
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def unread_count() -> int | None:
    cfg = _cfg()
    if not cfg or not is_configured():
        return None
    conn = _imap(cfg)
    try:
        typ, data = conn.uid("SEARCH", None, "UNSEEN")
        return len((data[0] or b"").split()) if typ == "OK" else 0
    finally:
        try:
            conn.logout()
        except Exception:
            pass


# ── Actions ──────────────────────────────────────────────────────────────────

def _check(conn, limit: int) -> str:
    typ, data = conn.uid("SEARCH", None, "UNSEEN")
    uids = (data[0] or b"").split() if typ == "OK" else []
    if not uids:
        return "No unread email, sir."
    lines = []
    for u in reversed(uids[-limit:]):
        msg = _fetch(conn, u.decode())
        if msg:
            lines.append(_line(_summary(u.decode(), msg),
                               _body(msg)[:140].replace("\n", " ")))
    more = f"\n(and {len(uids) - limit} older unread)" if len(uids) > limit else ""
    return f"{len(uids)} unread. Newest first:\n" + "\n".join(lines) + more


def _read(conn, uid: str) -> str:
    msg = _fetch(conn, uid)
    if not msg:
        return f"I couldn't find email {uid}, sir."
    s = _summary(uid, msg)
    body = _body(msg)
    if len(body) > 2500:
        body = body[:2500] + " … (truncated)"
    return f"From: {s['from']} <{s['address']}>\nSubject: {s['subject']}\nDate: {s['date']}\n\n{body}"


def _search(conn, query: str, limit: int) -> str:
    q = query.lower()
    typ, data = conn.uid("SEARCH", None, "ALL")
    uids = (data[0] or b"").split() if typ == "OK" else []
    hits = []
    for u in reversed(uids[-150:]):
        typ, d = conn.uid("FETCH", u.decode(), "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
        if typ != "OK" or not d or not isinstance(d[0], tuple):
            continue
        s = _summary(u.decode(), email.message_from_bytes(d[0][1]))
        if q in (s["from"] + " " + s["address"] + " " + s["subject"]).lower():
            hits.append(_line(s))
            if len(hits) >= limit:
                break
    return ("Matches:\n" + "\n".join(hits)) if hits else f"Nothing found for '{query}', sir."


def _deliver(cfg: dict, msg: EmailMessage) -> None:
    ctx = ssl.create_default_context()
    if cfg["smtp_host"].endswith(("office365.com", "mail.me.com")):
        with smtplib.SMTP(cfg["smtp_host"], 587, timeout=20) as s:
            s.starttls(context=ctx)
            s.login(cfg["address"], cfg["password"])
            s.send_message(msg)
    else:
        with smtplib.SMTP_SSL(cfg["smtp_host"], 465, context=ctx, timeout=20) as s:
            s.login(cfg["address"], cfg["password"])
            s.send_message(msg)


def _reply(cfg: dict, conn, uid: str, body: str, confirm: bool) -> str:
    if not body:
        return "What would you like the reply to say, sir?"
    orig = _fetch(conn, uid)
    if not orig:
        return f"I couldn't find email {uid}, sir."

    _, to_addr = parseaddr(_decode(orig.get("Reply-To") or orig.get("From")))
    subject = _decode(orig.get("Subject")) or ""
    subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"

    if not confirm:
        return (f"DRAFT — not sent yet.\nTo: {to_addr}\nSubject: {subject}\n\n{body}\n\n"
                "Read this back to the user and ask whether to send it. Only after "
                "a clear yes, call again with confirm=true.")

    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = cfg["address"], to_addr, subject
    msg["Date"], msg["Message-ID"] = formatdate(localtime=True), make_msgid()
    mid = orig.get("Message-ID")
    if mid:
        msg["In-Reply-To"] = mid
        msg["References"] = f"{orig.get('References', '')} {mid}".strip()
    msg.set_content(body)
    _deliver(cfg, msg)
    return f"Reply sent to {to_addr}, sir."


def _send(cfg: dict, to: str, subject: str, body: str, confirm: bool) -> str:
    if not to or not body:
        return "I need a recipient and a message, sir."
    if "@" not in to:
        return f"'{to}' isn't an email address. Ask the user for the address, or search their mail for it."
    if not confirm:
        return (f"DRAFT — not sent yet.\nTo: {to}\nSubject: {subject or '(no subject)'}\n\n{body}\n\n"
                "Read this back to the user and ask whether to send it. Only after "
                "a clear yes, call again with confirm=true.")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = cfg["address"], to, subject or "(no subject)"
    msg["Date"], msg["Message-ID"] = formatdate(localtime=True), make_msgid()
    msg.set_content(body)
    _deliver(cfg, msg)
    return f"Email sent to {to}, sir."


def email_assistant(parameters: dict, response=None, player=None, session_memory=None) -> str:
    """
    parameters:
        action   check (default) | read | search | reply | send
        uid      message id from a previous check/search      limit  how many to list
        query    text to find in sender or subject            body   reply / email text
        to, subject   for action=send                         confirm  true only after approval
    """
    p      = parameters or {}
    action = str(p.get("action") or "check").lower()
    cfg    = _cfg()
    if not cfg or not is_configured():
        return SETUP_HELP

    limit   = max(1, min(int(p.get("limit") or 5), 15))
    confirm = str(p.get("confirm", "")).lower() in ("true", "1", "yes")

    try:
        if player:
            player.write_log(f"[email] {action}")

        if action == "send":
            return _send(cfg, str(p.get("to") or "").strip(), p.get("subject") or "",
                         p.get("body") or "", confirm)

        conn = _imap(cfg)
        try:
            if action == "check":
                return _check(conn, limit)
            if action == "read":
                return _read(conn, str(p.get("uid") or ""))
            if action == "search":
                return _search(conn, str(p.get("query") or ""), limit)
            if action == "reply":
                return _reply(cfg, conn, str(p.get("uid") or ""), p.get("body") or "", confirm)
            return f"Unknown email action: {action}"
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    except imaplib.IMAP4.error:
        return "The mail server rejected the login, sir. Check the app password in config/api_keys.json."
    except (OSError, smtplib.SMTPException) as e:
        return f"I couldn't reach the mail server, sir: {str(e)[:80]}"
    except Exception as e:
        return f"Something went wrong with email: {str(e)[:80]}"
