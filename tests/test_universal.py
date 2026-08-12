"""The shell guard must stop a drive-wipe without blocking ordinary work."""

import pytest

from actions.universal import _DESTRUCTIVE

UNRECOVERABLE = [
    r"format C: /y",
    r"Format-Volume -DriveLetter D",
    r"diskpart /s script.txt",
    r"vssadmin delete shadows /all",
    r"bcdedit /set safeboot minimal",
    r"cipher /w:C",
    r"rd /s /q C:\ ".strip(),
    r"rmdir /s /q D:",
    r"del /f /s /q C:\*",
    r"Remove-Item C:\ -Recurse -Force",
    r"Get-Process; format D:",
]

ORDINARY = [
    r"echo hello",
    r"Get-ChildItem C:\Users",
    r"git status",
    r"Remove-Item C:\Users\me\project\build -Recurse -Force",
    r"rd /s /q C:\Users\me\temp",
    r"winget upgrade --all",
    r"pip install requests",
    r"Get-Content C:\log.txt",
    r"python main.py",
    r"Stop-Process -Name notepad",
    r"ipconfig /all",
]


@pytest.mark.parametrize("command", UNRECOVERABLE)
def test_unrecoverable_commands_are_held_back(command):
    assert _DESTRUCTIVE.search(command), f"guard missed: {command}"


@pytest.mark.parametrize("command", ORDINARY)
def test_ordinary_commands_pass(command):
    assert not _DESTRUCTIVE.search(command), f"guard blocked: {command}"
