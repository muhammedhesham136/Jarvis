# actions/email_checker.py
#
# Email over IMAP — an on-demand "check my email" tool plus a background
# watcher that polls the inbox and has JARVIS announce new mail on its own.
#
# Auth is a Gmail-style app password (config/api_keys.json: email_address /
# email_password), not OAuth — no browser consent flow, no client secret to
# provision, just IMAP over SSL. Works with any provider that issues app
# passwords; only the host/port default to Gmail.

import imaplib
import json
import threading
import time
from email import message_from_bytes
from email.header import decode_header
from pathlib import Path

BASE_DIR   = Path(__file__).resolve().parent.parent
CFG_PATH   = BASE_DIR / "config" / "api_keys.json"
STATE_PATH = BASE_DIR / "config" / ".email_state.json"

_DEFAULT_HOST     = "imap.gmail.com"
_DEFAULT_PORT     = 993
_DEFAULT_INTERVAL = 120


def _cfg() -> dict:
    try:
        return json.loads(CFG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _email_settings() -> dict | None:
    cfg     = _cfg()
    address = cfg.get("email_address", "").strip()
    password = cfg.get("email_password", "").strip()
    if not address or not password:
        return None
    return {
        "address":  address,
        "password": password,
        "host":     cfg.get("email_imap_host", _DEFAULT_HOST),
        "port":     int(cfg.get("email_imap_port", _DEFAULT_PORT)),
        "interval": max(30, int(cfg.get("email_check_interval_seconds", _DEFAULT_INTERVAL))),
        "announce": bool(cfg.get("email_announce", True)),
    }


def _decode(raw: str) -> str:
    if not raw:
        return ""
    parts = decode_header(raw)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            try:
                out.append(text.decode(enc or "utf-8", errors="replace"))
            except Exception:
                out.append(text.decode("utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out).strip()


def _connect(settings: dict) -> imaplib.IMAP4_SSL:
    imap = imaplib.IMAP4_SSL(settings["host"], settings["port"])
    imap.login(settings["address"], settings["password"])
    imap.select("INBOX")
    return imap


def _sender_name(raw_from: str) -> str:
    name = _decode(raw_from)
    if "<" in name:
        name = name.split("<")[0].strip().strip('"')
    return name or raw_from


def _fetch_headers(imap: imaplib.IMAP4_SSL, uid: bytes) -> dict | None:
    typ, data = imap.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
    if typ != "OK" or not data or not data[0]:
        return None
    raw = data[0][1] if isinstance(data[0], tuple) else data[0]
    msg = message_from_bytes(raw)
    return {
        "from":    _sender_name(msg.get("From", "")),
        "subject": _decode(msg.get("Subject", "")) or "(no subject)",
    }


def _highest_uid(imap: imaplib.IMAP4_SSL) -> int:
    typ, data = imap.uid("search", None, "ALL")
    if typ != "OK" or not data or not data[0]:
        return 0
    uids = data[0].split()
    return int(uids[-1]) if uids else 0


# ── On-demand tool ───────────────────────────────────────────────────────────

def check_email(parameters: dict, player=None, session_memory=None) -> str:
    """
    Reads the inbox over IMAP and returns a spoken summary.

    parameters:
        - unread_only (bool) default True
        - count       (int)  how many to summarise, default 5
    """
    settings = _email_settings()
    if not settings:
        return ("Email isn't set up yet, sir — add email_address and "
                "email_password to config/api_keys.json.")

    unread_only = parameters.get("unread_only", True)
    count = int(parameters.get("count", 5) or 5)
    count = max(1, min(count, 10))

    try:
        imap = _connect(settings)
    except imaplib.IMAP4.error as e:
        return f"I couldn't sign in to the inbox: {e}"
    except Exception as e:
        return f"I couldn't reach the mail server: {e}"

    try:
        typ, data = imap.uid("search", None, "UNSEEN" if unread_only else "ALL")
        if typ != "OK":
            return "I couldn't search the inbox just now."

        uids = data[0].split() if data and data[0] else []
        if not uids:
            return "No unread mail, sir." if unread_only else "The inbox is empty."

        chosen = list(reversed(uids))[:count]
        items = []
        for uid in chosen:
            info = _fetch_headers(imap, uid)
            if info:
                items.append(info)

        if not items:
            return "There's mail, but I couldn't read the headers."

        total = len(uids)
        lead = (f"You have {total} unread email{'s' if total != 1 else ''}. "
                if unread_only else f"Latest {len(items)} of {total} emails. ")
        body = " ".join(f"From {i['from']}: {i['subject']}." for i in items)
        if unread_only and total > len(items):
            body += f" And {total - len(items)} more."

        if player:
            player.write_log(f"[email] {total} matching, showed {len(items)}")
        return lead + body

    except Exception as e:
        return f"Something went wrong reading the inbox: {str(e)[:80]}"
    finally:
        try:
            imap.logout()
        except Exception:
            pass


# ── Background watcher ───────────────────────────────────────────────────────

def _load_last_uid() -> int | None:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8")).get("last_uid")
    except Exception:
        return None


def _save_last_uid(uid: int) -> None:
    try:
        STATE_PATH.write_text(json.dumps({"last_uid": uid}), encoding="utf-8")
    except Exception:
        pass


class EmailWatcher:
    """
    Polls the inbox on a timer and hands new mail to a speak callback.
    Silent on the very first poll (it only learns where "new" starts from
    there) so restarting JARVIS never dumps the whole backlog.
    """

    def __init__(self, speak, write_log=None):
        self._speak     = speak
        self._write_log = write_log or (lambda *_: None)
        self._stop      = threading.Event()
        self._thread    = None

    def start(self) -> bool:
        settings = _email_settings()
        if not settings or not settings["announce"]:
            return False
        self._thread = threading.Thread(target=self._run, args=(settings,), daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def _run(self, settings: dict) -> None:
        last_uid = _load_last_uid()
        backoff = settings["interval"]

        while not self._stop.is_set():
            try:
                imap = _connect(settings)
                try:
                    if last_uid is None:
                        last_uid = _highest_uid(imap)
                        _save_last_uid(last_uid)
                    else:
                        typ, data = imap.uid("search", None, f"(UID {last_uid + 1}:*)")
                        uids = [int(u) for u in data[0].split()] if typ == "OK" and data and data[0] else []
                        uids = [u for u in uids if u > last_uid]
                        if uids:
                            uids.sort()
                            items = []
                            for uid in uids[-5:]:
                                info = _fetch_headers(imap, str(uid).encode())
                                if info:
                                    items.append(info)
                            last_uid = uids[-1]
                            _save_last_uid(last_uid)
                            if items:
                                self._announce(items, len(uids))
                finally:
                    try:
                        imap.logout()
                    except Exception:
                        pass
                backoff = settings["interval"]
            except Exception as e:
                self._write_log(f"[email] watcher error: {str(e)[:80]}")
                backoff = min(backoff * 2, 900)

            self._stop.wait(backoff)

    def _announce(self, items: list, total: int) -> None:
        if len(items) == 1:
            text = f"Sir, you've just received an email from {items[0]['from']}: {items[0]['subject']}."
        else:
            parts = "; ".join(f"{i['from']} — {i['subject']}" for i in items)
            text = f"Sir, {total} new emails have arrived. {parts}."
        self._write_log(f"[email] announcing {total} new")
        self._speak(text)


def start_email_watcher(speak, write_log=None) -> EmailWatcher | None:
    watcher = EmailWatcher(speak, write_log=write_log)
    return watcher if watcher.start() else None
