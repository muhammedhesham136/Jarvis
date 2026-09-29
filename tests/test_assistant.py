"""Reminders, app matching and the email approval gate."""

from datetime import datetime, timedelta

import pytest

from core import reminder_store as store
from actions.open_app import best_match
from actions import email_assistant as mail
from actions.reminder import reminder


@pytest.fixture(autouse=True)
def temp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_PATH", tmp_path / "reminders.json")


def test_reminder_due_then_nagged_until_completed():
    past = datetime.now() - timedelta(minutes=1)
    item = store.add("call the bank", past)
    assert [i["id"] for i in store.due_now()] == [item["id"]]

    store.mark_fired(item["id"])
    assert store.due_now() == []                       # not due for a nag yet
    later = datetime.now() + timedelta(minutes=store.NAG_EVERY_MIN + 1)
    assert [i["id"] for i in store.due_now(later)] == [item["id"]]

    store.complete(item["id"])
    assert store.due_now(later) == []


def test_nagging_stops_after_limit():
    item = store.add("x", datetime.now() - timedelta(minutes=1))
    for _ in range(store.NAG_LIMIT + 1):
        store.mark_fired(item["id"])
    assert store.due_now(datetime.now() + timedelta(days=1)) == []
    assert store.active()                                # still open, just quiet


def test_repeating_reminder_rolls_forward():
    item = store.add("stand up", datetime.now() - timedelta(minutes=1), "daily")
    store.mark_fired(item["id"])
    nxt = store.active()[0]
    assert datetime.fromisoformat(nxt["due"]) > datetime.now()
    assert nxt["fired"] == 0


def test_todo_never_fires_and_is_found_by_words():
    store.add("buy milk and eggs")
    assert store.due_now() == []
    assert store.find("milk")["message"] == "buy milk and eggs"


def test_reminder_tool_flow():
    assert "past" in reminder({"message": "x", "date": "2000-01-01", "time": "10:00"})
    assert "Reminder set" in reminder({"message": "tea", "in_minutes": 30})
    assert "to-do" in reminder({"action": "add_task", "message": "renew passport"})
    assert "tea" in reminder({"action": "list"})
    assert "Marked done" in reminder({"action": "complete", "query": "passport"})
    assert "couldn't find" in reminder({"action": "delete", "query": "zebra"})


@pytest.mark.parametrize("query,names,expected", [
    ("whatsapp", ["WhatsApp", "WhatsApp Uninstall"], "WhatsApp"),
    ("code", ["Visual Studio Code", "Codecademy"], "Visual Studio Code"),
    ("visual studio", ["Visual Studio Code", "Visual Studio Installer"], "Visual Studio Code"),
    ("photoshop", ["Word", "Excel"], None),
    ("powerpoint", ["Microsoft PowerPoint", "Word"], "Microsoft PowerPoint"),
])
def test_best_match(query, names, expected):
    assert best_match(query, names) == expected


def test_email_unconfigured_explains_setup(monkeypatch):
    monkeypatch.setattr(mail, "_cfg", lambda: None)
    assert "app password" in mail.email_assistant({"action": "check"})


def test_email_send_needs_confirmation(monkeypatch):
    sent = []
    cfg = {"address": "me@gmail.com", "password": "x",
           "imap_host": "imap.gmail.com", "smtp_host": "smtp.gmail.com"}
    monkeypatch.setattr(mail, "_cfg", lambda: cfg)
    monkeypatch.setattr(mail, "_deliver", lambda c, m: sent.append(m))

    draft = mail.email_assistant({"action": "send", "to": "a@b.com", "body": "hi"})
    assert "DRAFT" in draft and not sent

    done = mail.email_assistant({"action": "send", "to": "a@b.com", "body": "hi",
                                 "confirm": True})
    assert "sent" in done and len(sent) == 1

    assert "isn't an email address" in mail.email_assistant(
        {"action": "send", "to": "Ahmed", "body": "hi", "confirm": True})


def test_body_drops_quoted_history_and_html():
    import email as em
    raw = ("Subject: t\nContent-Type: text/html\n\n"
           "<p>Sure, Friday works.</p><br>On Mon, Bob wrote:<br>old stuff")
    assert mail._body(em.message_from_string(raw)) == "Sure, Friday works."


# ── Local Outlook route (fake COM objects) ───────────────────────────────────

from actions import outlook_mail as ol


class FakeItem:
    Class = 43

    def __init__(self, eid, sender, subject, body, when, unread=True):
        self.EntryID, self.SenderName, self.Subject = eid, sender, subject
        self.Body, self.ReceivedTime, self.UnRead = body, when, unread
        self.SenderEmailAddress, self.SenderEmailType = f"{sender.lower()}@x.com", "SMTP"
        self.replied = None

    def Reply(self):
        outer = self

        class R:
            Body = "\n> original"

            def Send(self_inner):
                outer.replied = self_inner.Body
        return R()


class FakeItems(list):
    def Sort(self, *a):
        self.sort(key=lambda i: i.ReceivedTime, reverse=True)


class FakeFolder:
    def __init__(self, items):
        self.Items = FakeItems(items)
        self.UnReadItemCount = sum(1 for i in items if i.UnRead)


@pytest.fixture
def outlook(monkeypatch, tmp_path):
    now = datetime.now()
    items = [
        FakeItem("E1", "Sara", "Lunch Friday?", "Are you free Friday?\nOn Mon, Bob wrote:\nold", now - timedelta(hours=2)),
        FakeItem("E2", "Bank", "Statement", "Your statement is ready", now - timedelta(hours=1)),
        FakeItem("E3", "Old", "Read already", "x", now - timedelta(days=1), unread=False),
    ]
    folder = FakeFolder(items)
    monkeypatch.setattr(ol, "_inbox", lambda: folder)
    monkeypatch.setattr(ol, "STATE_PATH", tmp_path / "o.json")
    monkeypatch.setattr(ol, "_ids", {})
    monkeypatch.setattr(ol, "_find", lambda uid: next(
        (i for i in folder.Items if i.EntryID == ol._ids.get(str(uid))), None))
    monkeypatch.setattr(mail, "_backend", lambda: "outlook")
    return items, folder


def test_outlook_check_lists_unread_newest_first_without_quoted_history(outlook):
    out = mail.email_assistant({"action": "check"})
    assert out.startswith("2 unread")
    assert out.index("Bank") < out.index("Sara")
    assert "Read already" not in out and "wrote" not in out


def test_outlook_read_and_search(outlook):
    mail.email_assistant({"action": "check"})
    assert "Are you free Friday?" in mail.email_assistant({"action": "read", "uid": "2"})
    assert "Lunch Friday?" in mail.email_assistant({"action": "search", "query": "sara"})
    assert "Nothing found" in mail.email_assistant({"action": "search", "query": "zebra"})


def test_outlook_reply_needs_confirmation(outlook):
    items, _ = outlook
    mail.email_assistant({"action": "check"})
    uid = next(k for k, v in ol._ids.items() if v == "E1")
    monkey_item = items[0]
    draft = mail.email_assistant({"action": "reply", "uid": uid, "body": "Yes, Friday works"})
    assert "DRAFT" in draft and monkey_item.replied is None
    mail.email_assistant({"action": "reply", "uid": uid, "body": "Yes, Friday works",
                          "confirm": True})
    assert monkey_item.replied.startswith("Yes, Friday works")


def test_outlook_new_mail_watcher_baselines_then_reports_only_new(outlook):
    items, folder = outlook
    assert ol.new_since_last_check()["count"] == 0          # baseline, no flood
    assert ol.new_since_last_check()["count"] == 0
    folder.Items.append(FakeItem("E4", "Boss", "Urgent", "call me", datetime.now()))
    res = ol.new_since_last_check()
    assert res["count"] == 1 and "Boss" in res["items"][0]
    assert ol.new_since_last_check()["count"] == 0


def test_backend_falls_back_to_outlook_only_without_login(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(mail, "_cfg", lambda: None)
    monkeypatch.setattr(ol, "is_available", lambda: True)
    assert mail._backend() == "outlook"
    monkeypatch.setattr(ol, "is_available", lambda: False)
    assert mail._backend() is None
