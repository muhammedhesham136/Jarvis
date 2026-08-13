"""
Universal execution — the fallback that lets JARVIS act on anything.

The rest of the action modules cover known jobs. These two cover the rest,
so a request never has to be answered with "I can't do that":

  run_command   run a shell command directly and report what it printed
  do_anything   have the model write a Python program for the goal, run it,
                and if it fails hand the traceback back for a repair pass

``do_anything`` is deliberately the last resort in the prompt's tool order:
it is the most capable and the slowest.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR    = get_base_dir()
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
SCRATCH     = BASE_DIR / "_scratch"

MAX_REPAIRS  = 3
RUN_TIMEOUT  = 180
SPOKEN_LIMIT = 1600     # results are read aloud, so keep them short

# Windows hides consoles with this flag; without it every call flashes a window.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Commands that could wipe a disk or the boot config. The model is good but
# not infallible, and a spoken misunderstanding should never cost a drive.
#
# Deleting a folder recursively is a perfectly ordinary request, so only the
# genuinely unrecoverable shapes are held back: wiping a whole drive, or
# destroying the partition table, boot config, or shadow copies.
#
_DESTRUCTIVE = re.compile(
    r"""(?ix)
    (?: ^ | [\s;|&(] )                      # at the start of a command
    (?:
        format \s+ [a-z]:                                       |
        format-volume                                           |
        clear-disk                                              |
        diskpart                                                |
        mkfs                                                    |
        bcdedit                                                 |
        cipher \s+ /w                                           |
        vssadmin \s+ delete                                     |
        (?:rd|rmdir) \s+ (?: /[a-z] \s+ )* ["']? [a-z]:\\? ["']? \s* $   |
        del \s+ (?: /[a-z] \s+ )* ["']? [a-z]:\\ \* |
        remove-item \s+ (?: -path \s+ )? ["']? [a-z]:\\? ["']? (?: \s | $ )
    )
    """
)


def _cfg() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _log(player, text: str):
    print(f"[Universal] {text}")
    if player is not None:
        try:
            player.write_log(text)
        except Exception:
            pass


def _trim(text: str, limit: int = SPOKEN_LIMIT) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " …(truncated)"


# ══════════════════════════════════════════════════════════════════════════
#  run_command
# ══════════════════════════════════════════════════════════════════════════

def run_command(parameters: dict | None = None, player=None, **_kw) -> str:
    """Run a shell command and return whatever it printed."""
    p       = parameters or {}
    command = (p.get("command") or "").strip()
    shell   = (p.get("shell") or "powershell").lower()
    timeout = int(p.get("timeout") or 90)

    if not command:
        return "No command was given."

    if _DESTRUCTIVE.search(command) and not p.get("confirm_destructive"):
        return (
            "That command would erase a disk or the boot configuration, so I "
            "have held it back. Say it again and confirm explicitly if you "
            "truly want it run."
        )

    _log(player, f"CMD: {command[:110]}")

    if shell.startswith("cmd"):
        argv = ["cmd.exe", "/c", command]
    else:
        argv = ["powershell.exe", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", command]

    try:
        r = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
            cwd=str(Path.home()), creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return f"The command was still running after {timeout} seconds, so I stopped it."
    except Exception as e:
        return f"The command could not be started: {e}"

    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()

    if r.returncode != 0:
        return _trim(f"Exit code {r.returncode}. {err or out or 'No output.'}")
    return _trim(out or "Done — the command produced no output.")


# ══════════════════════════════════════════════════════════════════════════
#  do_anything
# ══════════════════════════════════════════════════════════════════════════

_CODE_PROMPT = """You write Python programs that run on the user's own Windows 11 machine.

TASK
{goal}

ENVIRONMENT
- Python {pyver} on Windows 11. The script runs with full user privileges.
- Home: {home}
- Desktop: {desktop}
- Downloads: {downloads}
- Documents: {documents}
- Installed and importable: requests, bs4, PIL, numpy, cv2, mss, psutil,
  pyautogui, pyperclip, pygetwindow, pywinauto, send2trash, comtypes, pycaw,
  playwright, duckduckgo_search, youtube_transcript_api
- Anything else must come from the standard library, or you may pip install it
  with subprocess as part of the script.

RULES
- Output ONLY Python source. No markdown fences, no commentary.
- The script must be complete and runnable exactly as written.
- print() a short, human-readable statement of the RESULT. That text is read
  aloud, so keep it to one or two sentences and never print raw data dumps.
- Never call input() or anything else that waits for the user.
- Never loop forever; finish within {timeout} seconds.
- Use subprocess for system operations. Use pyautogui only if the task truly
  needs mouse or keyboard control.
- Do not delete or overwrite the user's files unless the task explicitly says to.
- Wrap risky sections in try/except and print what went wrong.
"""

_REPAIR_PROMPT = """The program you wrote failed. Fix it and return the corrected program.

TASK
{goal}

PROGRAM
{code}

ERROR
{error}

Output ONLY the corrected Python source. No fences, no commentary.
"""


def _model():
    from core import genai_compat as genai
    cfg = _cfg()
    genai.configure(api_key=cfg.get("gemini_api_key", ""))
    return genai.GenerativeModel(cfg.get("model_name", "gemini-3.5-flash"))


def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    if "```" in text:
        blocks = re.findall(r"```(?:python)?\s*(.*?)```", text, re.S)
        if blocks:
            text = max(blocks, key=len)
    return text.strip()


def _write_script(code: str) -> Path:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(suffix=".py", dir=str(SCRATCH), text=True)
    os.close(fd)
    path = Path(name)
    path.write_text(code, encoding="utf-8")
    return path


def _run_script(path: Path, timeout: int) -> tuple[int, str, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"      # child would otherwise die on unicode
    env["PYTHONUTF8"] = "1"

    r = subprocess.run(
        [sys.executable, str(path)],
        capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace",
        cwd=str(Path.home()), env=env, creationflags=_NO_WINDOW,
    )
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def do_anything(parameters: dict | None = None, player=None, speak=None, **_kw) -> str:
    """Write a program for the goal, run it, and repair it if it fails."""
    p       = parameters or {}
    goal    = (p.get("goal") or p.get("description") or "").strip()
    timeout = int(p.get("timeout") or RUN_TIMEOUT)

    if not goal:
        return "No goal was given."

    _log(player, f"TASK: {goal[:110]}")

    home = Path.home()
    try:
        model = _model()
    except Exception as e:
        return f"I could not reach the model to plan this: {e}"

    prompt = _CODE_PROMPT.format(
        goal=goal,
        pyver=f"{sys.version_info.major}.{sys.version_info.minor}",
        home=home,
        desktop=home / "Desktop",
        downloads=home / "Downloads",
        documents=home / "Documents",
        timeout=timeout,
    )

    code = ""
    last_error = ""

    for attempt in range(1, MAX_REPAIRS + 1):
        try:
            if attempt == 1:
                code = _strip_fences(model.generate_content(prompt).text)
            else:
                code = _strip_fences(model.generate_content(
                    _REPAIR_PROMPT.format(goal=goal, code=code,
                                          error=last_error[:1800])
                ).text)
        except Exception as e:
            return f"I could not write the program for that: {e}"

        if not code:
            last_error = "The model returned an empty program."
            continue

        path = _write_script(code)
        _log(player, f"RUN: attempt {attempt} — {path.name}")

        try:
            rc, out, err = _run_script(path, timeout)
        except subprocess.TimeoutExpired:
            last_error = f"The program was still running after {timeout} seconds."
            _log(player, f"TIMEOUT on attempt {attempt}")
            continue
        except Exception as e:
            last_error = str(e)
            continue
        finally:
            try:
                path.unlink()
            except Exception:
                pass

        if rc == 0:
            _log(player, f"OK on attempt {attempt}")
            return _trim(out or "Done.")

        last_error = err or out or f"Exit code {rc}."
        _log(player, f"FAIL attempt {attempt}: {last_error[:140]}")
        if attempt < MAX_REPAIRS and speak:
            try:
                speak("That approach did not hold. Adjusting, sir.")
            except Exception:
                pass

    return _trim(
        f"I tried {MAX_REPAIRS} approaches and none completed. "
        f"The last error was: {last_error}", 600
    )
