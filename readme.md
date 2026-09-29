# J.A.R.V.I.S.

A real-time voice assistant for Windows. It listens, answers in a calm British
voice, and actually carries out what you ask it to do on your machine — no
subscription, running locally against your own Gemini key.

The front end is a 3D WebGL reactor and nothing else. No panels, no labels, no
log. What it is doing is written in colour and motion:

| | |
|---|---|
| **cyan, slow breathing** | listening |
| **bright white-cyan, spectrum alive** | speaking — the ring is driven by the real audio |
| **amber, faster rotation** | thinking, or a tool is running |
| **deep red, almost still** | muted |

---

## Quick start

```bash
pip install -r requirements.txt
playwright install
```

Then start it with **`run.bat`** — it launches the interpreter in `.venv`, where
the WebGL packages live. Starting `main.py` with a different Python still runs,
but silently falls back to the flat renderer without the 3D interface.

Paste your [Gemini API key](https://aistudio.google.com/apikey) on first launch.

**Requirements** — Windows 10/11, Python 3.11+, a microphone, and a free Gemini
API key.

---

## Using it

Just talk. It is always listening unless muted.

| Key | |
|---|---|
| *start typing* | a command bar fades in — type an order instead of speaking |
| `Enter` | send |
| `Esc` | dismiss the bar, or drop to the tray if it is already closed |
| `F4` | mute / unmute the microphone |
| `Ctrl+Q` | shut down completely |

### Running in the background

Press `Esc` (or close the window) and JARVIS slips into the system tray, still
listening while you use the machine. The tray icon — cyan when live, red when
muted — right-clicks to a menu:

| | |
|---|---|
| **Show JARVIS** | bring the window back (or double-click the icon) |
| **Microphone muted** | mute / unmute without opening the window |
| **Quit JARVIS** | shut down completely |

---

## What it can do

Opening apps, controlling volume and brightness and windows, managing files and
folders, driving the browser, searching the web, playing and summarising videos,
reading your screen or webcam, sending WhatsApp and Telegram messages, setting
reminders, checking weather and flights, installing and updating Steam and Epic
games, writing and running code, and building whole projects from nothing.

It also remembers you — your name, where you live, what you are working on,
who matters to you — and carries that between sessions.

### Email, reminders and the briefing

Add your mail login to `config/api_keys.json` (Gmail, Outlook, Yahoo and iCloud
work out of the box; use an **app password**, not your normal one — for Gmail
turn on 2-step verification, then create one at
[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)):

```json
"email_address": "you@gmail.com",
"email_password": "abcd efgh ijkl mnop"
```

- *"Any new emails?"* / *"Read the one from Sara"* / *"Reply, tell her Friday works."*
  He reads the draft back and only sends after you say yes.
- New mail is announced on its own, every few minutes.
- *"Remind me at 5 to call the bank"*, *"remind me every weekday to stand up"*,
  *"I mustn't forget to renew my passport"* (a to-do). Reminders are spoken when
  due and repeated up to three times until you say *done* or *snooze it*.
- Anything that fell due while JARVIS was closed is announced at the next start,
  together with open to-dos and unread mail.

### Anything else

The tools above cover the common jobs. `do_anything` covers the rest: it writes
a Python program for whatever you asked, runs it on your machine, reads the
traceback if it fails, and repairs it. That is why an order rarely comes back
as "I can't".

---

## How it is put together

```
main.py              live session, mic capture, tool dispatch
ui.py                hosts the WebGL window, talks to it over a local socket
ui_web/index.html    the reactor — one WebGL2 shader, no dependencies
ui_tk.py             fallback renderer if WebView2 is missing
core/prompt.txt      who JARVIS is and how he speaks
actions/             one module per capability
  universal.py       run_command and do_anything
agent/               planner, executor, task queue for multi-step goals
memory/              long-term memory
```

Three threads: the window owns the main thread, the Gemini session and
microphone run on an asyncio thread, and a third thread does nothing but push
audio to the speakers. Tool calls are dispatched as background tasks, so a slow
browser or shell job never stalls speech.

The reactor is drawn analytically rather than raymarched — each ring is one
ray/plane intersection and the core is a closed-form glow — so it holds 60fps
on integrated graphics.

### Settings

`config/api_keys.json`:

```json
{
    "gemini_api_key": "…",
    "model_name": "gemini-2.5-flash",
    "voice_name": "Charon"
}
```

`voice_name` accepts any Gemini live voice — `Charon`, `Orus`, `Rasalgethi`,
`Alnilam`, `Iapetus` and others. The accent and delivery come from
`core/prompt.txt`, so edit that if you want him to sound different.

---

## Credit

Built on [Mark-XXXV](https://github.com/FatihMakes/Mark-XXXV) by
[FatihMakes](https://www.youtube.com/@FatihMakes), under CC BY-NC 4.0 —
personal and non-commercial use.
