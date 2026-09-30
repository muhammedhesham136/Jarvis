"""Risky actions must be caught and gated; ordinary work must not be nagged."""

import pytest

from core import safety
from actions import universal

RISKY = {
    "import shutil\nshutil.rmtree('C:/Users/me/x')":            "delete",
    "import os\nos.remove('a.txt')":                            "delete",
    "Remove-Item C:\\Users\\me\\old -Recurse":                  "delete",
    "pip install requests":                                     "install",
    "winget install VLC.VLC":                                   "install",
    "import smtplib":                                           "send data",
    "requests.post('https://x.com', data=d)":                   "send data",
    "reg add HKLM\\Software\\X /v a":                           "registry",
    "shutdown /s /t 0":                                         "shut down",
    "taskkill /f /im chrome.exe":                               "shut down",
    "exec(base64.b64decode(blob))":                             "downloaded or encoded",
    "shutil.move('a', 'b')":                                    "move",
}

SAFE = [
    "import requests\nprint(requests.get('https://wttr.in').text[:50])",
    "Get-ChildItem C:\\Users",
    "import psutil\nprint(psutil.cpu_percent())",
    "with open('report.txt','w') as f: f.write('hi')",
    "print('hello')",
    "git status",
]


@pytest.mark.parametrize("text,word", RISKY.items())
def test_risky_actions_are_flagged(text, word):
    assert any(word in r for r in safety.risks(text)), text


@pytest.mark.parametrize("text", SAFE)
def test_ordinary_work_is_not_flagged(text):
    assert safety.risks(text) == []


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(safety, "LOG_PATH", tmp_path / "activity.jsonl")
    monkeypatch.setattr(safety, "_enabled", lambda: True)


def test_no_dialog_when_nothing_is_risky(sandbox, monkeypatch):
    monkeypatch.setattr(safety, "_dialog", lambda *a: pytest.fail("asked needlessly"))
    assert safety.approve("a command", "echo hi", [])


def test_declined_command_is_not_run_and_is_logged(sandbox, monkeypatch):
    ran = []
    monkeypatch.setattr(safety, "_dialog", lambda *a: False)
    monkeypatch.setattr(universal, "_run_shell", lambda *a: ran.append(a) or "ran")
    out = universal.run_command({"command": "Remove-Item C:\\Users\\me\\old -Recurse"})
    assert "did not approve" in out and not ran
    assert safety.recent()[-1]["approved"] is False


def test_approved_command_runs_and_is_logged(sandbox, monkeypatch):
    monkeypatch.setattr(safety, "_dialog", lambda *a: True)
    monkeypatch.setattr(universal, "_run_shell", lambda *a: "deleted")
    assert universal.run_command({"command": "Remove-Item C:\\x"}) == "deleted"
    assert safety.recent()[-1]["approved"] is True


def test_drive_wipe_asks_even_when_prompts_are_off(sandbox, monkeypatch):
    monkeypatch.setattr(safety, "_enabled", lambda: False)
    asked = []
    monkeypatch.setattr(safety, "_dialog", lambda *a: asked.append(1) or False)
    out = universal.run_command({"command": "format D: /y"})
    assert asked and "did not approve" in out


def test_prompts_can_be_turned_off_for_ordinary_risks(sandbox, monkeypatch):
    monkeypatch.setattr(safety, "_enabled", lambda: False)
    monkeypatch.setattr(safety, "_dialog", lambda *a: pytest.fail("asked"))
    assert safety.approve("a command", "pip install x", ["install or remove software"])


def test_model_cannot_self_approve(sandbox, monkeypatch):
    # An old-style flag in the tool arguments must not bypass the dialog.
    monkeypatch.setattr(safety, "_dialog", lambda *a: False)
    monkeypatch.setattr(universal, "_run_shell", lambda *a: "ran")
    out = universal.run_command({"command": "format D: /y", "confirm_destructive": True,
                                 "confirmed": True})
    assert "did not approve" in out


def test_do_anything_declined_program_never_runs(sandbox, monkeypatch):
    class M:
        def generate_content(self, _):
            class R: text = "import shutil\nshutil.rmtree('C:/Users/me/x')"
            return R()
    ran = []
    monkeypatch.setattr(universal, "_model", lambda: M())
    monkeypatch.setattr(universal, "_run_script", lambda *a: ran.append(a) or (0, "x", ""))
    monkeypatch.setattr(safety, "_dialog", lambda *a: False)
    out = universal.do_anything({"goal": "clean my folder"})
    assert "did not approve" in out and not ran
