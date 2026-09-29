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
