"""
J.A.R.V.I.S — live voice assistant.

Threading model
---------------
main thread      the WebGL window (blocking, owned by ui.start())
asyncio thread   the Gemini Live session, mic capture, tool dispatch
playback thread  drains the audio queue into the speakers

Two rules keep it responsive:

1. The receive loop never blocks. Tool calls are dispatched as background
   tasks and their responses are sent when they finish, so a slow browser
   or shell job cannot stall speech.
2. Nothing that touches audio ever raises into the event loop. The mic
   queue drops its oldest frame rather than overflowing, and every tool is
   wrapped so a failure comes back as a spoken sentence instead of a
   dropped session.
"""

import asyncio
import json
import queue
import sys
import threading
import traceback
from pathlib import Path

# Windows consoles default to the ANSI code page, so one non-Latin character
# in a transcript — Arabic, Turkish, an emoji — would raise UnicodeEncodeError
# from inside the receive loop and take the whole session down with it.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import numpy as np
import sounddevice as sd
from google import genai
from google.genai import types

from ui import JarvisUI
from memory.memory_manager import (
    load_memory, update_memory, format_memory_for_prompt,
    should_extract_memory, extract_memory
)

from actions.flight_finder     import flight_finder
from actions.open_app          import open_app
from actions.weather_report    import weather_action
from actions.send_message      import send_message
from actions.reminder          import reminder
from actions.computer_settings import computer_settings
from actions.screen_processor  import screen_process
from actions.youtube_video     import youtube_video
from actions.cmd_control       import cmd_control
from actions.desktop           import desktop_control
from actions.browser_control   import browser_control
from actions.file_controller   import file_controller
from actions.code_helper       import code_helper
from actions.dev_agent         import dev_agent
from actions.web_search        import web_search as web_search_action
from actions.computer_control  import computer_control
from actions.game_updater      import game_updater
from actions.universal         import do_anything, run_command


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"

LIVE_MODEL   = "models/gemini-2.5-flash-native-audio-latest"
CHANNELS     = 1
SEND_RATE    = 16000
RECV_RATE    = 24000

# 32 ms of audio per frame. The old 8 ms blocks fired the callback 125×/s and
# swamped the send queue for no latency benefit.
IN_BLOCK     = 512
MIC_QUEUE    = 32          # ~1 s of slack; oldest frame is dropped when full
PLAY_IDLE    = 0.12        # silence for this long ⇒ the turn has finished


def _cfg() -> dict:
    try:
        return json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _get_api_key() -> str:
    return _cfg().get("gemini_api_key", "")


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are JARVIS, the user's personal assistant. Address him as sir. "
            "Speak with a refined British accent, calm and concise. Always use "
            "the provided tools to carry out an order, and never simulate a result."
        )


def _rms(pcm: bytes) -> float:
    """Perceptual 0..1 level from int16 PCM, for the reactor animation."""
    try:
        a = np.frombuffer(pcm, dtype=np.int16)
        if a.size == 0:
            return 0.0
        r = float(np.sqrt(np.mean(np.square(a.astype(np.float32) / 32768.0))))
        return float(min(1.0, (r * 5.0) ** 0.7))
    except Exception:
        return 0.0


# ── Memory ────────────────────────────────────────────────────────────────────
_last_memory_input = ""


def _update_memory_async(user_text: str, jarvis_text: str) -> None:
    global _last_memory_input

    user_text   = (user_text   or "").strip()
    jarvis_text = (jarvis_text or "").strip()

    if len(user_text) < 5 or user_text == _last_memory_input:
        return
    _last_memory_input = user_text

    try:
        api_key = _get_api_key()
        if not should_extract_memory(user_text, jarvis_text, api_key):
            return
        data = extract_memory(user_text, jarvis_text, api_key)
        if data:
            update_memory(data)
            print(f"[Memory] saved {list(data.keys())}")
    except Exception as e:
        if "429" not in str(e):
            print(f"[Memory] {e}")


# ── Tool declarations ─────────────────────────────────────────────────────────
TOOL_DECLARATIONS = [
    {
        "name": "open_app",
        "description": (
            "Opens any application on the Windows computer. "
            "Use this whenever the user asks to open, launch, or start any app, "
            "website, or program. Always call this tool — never just say you opened it."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "app_name": {
                    "type": "STRING",
                    "description": "Exact name of the application (e.g. 'WhatsApp', 'Chrome', 'Spotify')"
                }
            },
            "required": ["app_name"]
        }
    },
    {
        "name": "web_search",
        "description": "Searches the web for any information.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query":  {"type": "STRING", "description": "Search query"},
                "mode":   {"type": "STRING", "description": "search (default) or compare"},
                "items":  {"type": "ARRAY", "items": {"type": "STRING"}, "description": "Items to compare"},
                "aspect": {"type": "STRING", "description": "price | specs | reviews"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "weather_report",
        "description": "Gets real-time weather information for a city.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "city": {"type": "STRING", "description": "City name"}
            },
            "required": ["city"]
        }
    },
    {
        "name": "send_message",
        "description": "Sends a text message via WhatsApp, Telegram, or other messaging platform.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "receiver":     {"type": "STRING", "description": "Recipient contact name"},
                "message_text": {"type": "STRING", "description": "The message to send"},
                "platform":     {"type": "STRING", "description": "Platform: WhatsApp, Telegram, etc."}
            },
            "required": ["receiver", "message_text", "platform"]
        }
    },
    {
        "name": "reminder",
        "description": "Sets a timed reminder using Windows Task Scheduler.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "date":    {"type": "STRING", "description": "Date in YYYY-MM-DD format"},
                "time":    {"type": "STRING", "description": "Time in HH:MM format (24h)"},
                "message": {"type": "STRING", "description": "Reminder message text"}
            },
            "required": ["date", "time", "message"]
        }
    },
    {
        "name": "youtube_video",
        "description": (
            "Controls YouTube. Use for: playing videos, summarizing a video's content, "
            "getting video info, or showing trending videos."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "play | summarize | get_info | trending (default: play)"},
                "query":  {"type": "STRING", "description": "Search query for play action"},
                "save":   {"type": "BOOLEAN", "description": "Save summary to Notepad (summarize only)"},
                "region": {"type": "STRING", "description": "Country code for trending e.g. TR, US"},
                "url":    {"type": "STRING", "description": "Video URL for get_info action"},
            },
            "required": []
        }
    },
    {
        "name": "screen_process",
        "description": (
            "Captures and analyzes the screen or webcam image. "
            "MUST be called when user asks what is on screen, what you see, "
            "analyze my screen, look at camera, etc. "
            "You have NO visual ability without this tool. "
            "After calling this tool, stay SILENT — the vision module speaks directly."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "angle": {"type": "STRING", "description": "'screen' to capture display, 'camera' for webcam. Default: 'screen'"},
                "text":  {"type": "STRING", "description": "The question or instruction about the captured image"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "computer_settings",
        "description": (
            "Controls the computer: volume, brightness, window management, keyboard shortcuts, "
            "typing text on screen, closing apps, fullscreen, dark mode, WiFi, restart, shutdown, "
            "scrolling, tab management, zoom, screenshots, lock screen, refresh/reload page. "
            "Use for ANY single computer control command. NEVER route to agent_task."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "The action to perform"},
                "description": {"type": "STRING", "description": "Natural language description of what to do"},
                "value":       {"type": "STRING", "description": "Optional value: volume level, text to type, etc."}
            },
            "required": []
        }
    },
    {
        "name": "browser_control",
        "description": (
            "Controls the web browser. Use for: opening websites, searching the web, "
            "clicking elements, filling forms, scrolling, any web-based task."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "go_to | search | click | type | scroll | fill_form | smart_click | smart_type | get_text | press | close"},
                "url":         {"type": "STRING", "description": "URL for go_to action"},
                "query":       {"type": "STRING", "description": "Search query for search action"},
                "selector":    {"type": "STRING", "description": "CSS selector for click/type"},
                "text":        {"type": "STRING", "description": "Text to click or type"},
                "description": {"type": "STRING", "description": "Element description for smart_click/smart_type"},
                "direction":   {"type": "STRING", "description": "up or down for scroll"},
                "key":         {"type": "STRING", "description": "Key name for press action"},
                "incognito":   {"type": "BOOLEAN", "description": "Open in private/incognito mode"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "file_controller",
        "description": "Manages files and folders: list, create, delete, move, copy, rename, read, write, find, disk usage.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "list | create_file | create_folder | delete | move | copy | rename | read | write | find | largest | disk_usage | organize_desktop | info"},
                "path":        {"type": "STRING", "description": "File/folder path or shortcut: desktop, downloads, documents, home"},
                "destination": {"type": "STRING", "description": "Destination path for move/copy"},
                "new_name":    {"type": "STRING", "description": "New name for rename"},
                "content":     {"type": "STRING", "description": "Content for create_file/write"},
                "name":        {"type": "STRING", "description": "File name to search for"},
                "extension":   {"type": "STRING", "description": "File extension to search (e.g. .pdf)"},
                "count":       {"type": "INTEGER", "description": "Number of results for largest"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "cmd_control",
        "description": (
            "Runs CMD/terminal commands via natural language: disk space, processes, "
            "system info, network, find files, or anything in the command line."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "task":    {"type": "STRING", "description": "Natural language description of what to do"},
                "visible": {"type": "BOOLEAN", "description": "Open visible CMD window. Default: true"},
                "command": {"type": "STRING", "description": "Optional: exact command if already known"},
            },
            "required": ["task"]
        }
    },
    {
        "name": "desktop_control",
        "description": "Controls the desktop: wallpaper, organize, clean, list, stats.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "wallpaper | wallpaper_url | organize | clean | list | stats | task"},
                "path":   {"type": "STRING", "description": "Image path for wallpaper"},
                "url":    {"type": "STRING", "description": "Image URL for wallpaper_url"},
                "mode":   {"type": "STRING", "description": "by_type or by_date for organize"},
                "task":   {"type": "STRING", "description": "Natural language desktop task"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "code_helper",
        "description": "Writes, edits, explains, runs, or builds code files.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "write | edit | explain | run | build | auto (default: auto)"},
                "description": {"type": "STRING", "description": "What the code should do or what change to make"},
                "language":    {"type": "STRING", "description": "Programming language (default: python)"},
                "output_path": {"type": "STRING", "description": "Where to save the file"},
                "file_path":   {"type": "STRING", "description": "Path to existing file for edit/explain/run/build"},
                "code":        {"type": "STRING", "description": "Raw code string for explain"},
                "args":        {"type": "STRING", "description": "CLI arguments for run/build"},
                "timeout":     {"type": "INTEGER", "description": "Execution timeout in seconds (default: 30)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "dev_agent",
        "description": "Builds complete multi-file projects from scratch: plans, writes files, installs deps, opens VSCode, runs and fixes errors.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "description":  {"type": "STRING", "description": "What the project should do"},
                "language":     {"type": "STRING", "description": "Programming language (default: python)"},
                "project_name": {"type": "STRING", "description": "Optional project folder name"},
                "timeout":      {"type": "INTEGER", "description": "Run timeout in seconds (default: 30)"},
            },
            "required": ["description"]
        }
    },
    {
        "name": "agent_task",
        "description": (
            "Executes complex multi-step tasks requiring multiple different tools. "
            "Examples: 'research X and save to file', 'find and organize files'. "
            "DO NOT use for single commands. NEVER use for Steam/Epic — use game_updater."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "goal":     {"type": "STRING", "description": "Complete description of what to accomplish"},
                "priority": {"type": "STRING", "description": "low | normal | high (default: normal)"}
            },
            "required": ["goal"]
        }
    },
    {
        "name": "computer_control",
        "description": "Direct computer control: type, click, hotkeys, scroll, move mouse, screenshots, find elements on screen.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "type | smart_type | click | double_click | right_click | hotkey | press | scroll | move | copy | paste | screenshot | wait | clear_field | focus_window | screen_find | screen_click | random_data | user_data"},
                "text":        {"type": "STRING", "description": "Text to type or paste"},
                "x":           {"type": "INTEGER", "description": "X coordinate"},
                "y":           {"type": "INTEGER", "description": "Y coordinate"},
                "keys":        {"type": "STRING", "description": "Key combination e.g. 'ctrl+c'"},
                "key":         {"type": "STRING", "description": "Single key e.g. 'enter'"},
                "direction":   {"type": "STRING", "description": "up | down | left | right"},
                "amount":      {"type": "INTEGER", "description": "Scroll amount (default: 3)"},
                "seconds":     {"type": "NUMBER",  "description": "Seconds to wait"},
                "title":       {"type": "STRING",  "description": "Window title for focus_window"},
                "description": {"type": "STRING",  "description": "Element description for screen_find/screen_click"},
                "type":        {"type": "STRING",  "description": "Data type for random_data"},
                "field":       {"type": "STRING",  "description": "Field for user_data: name|email|city"},
                "clear_first": {"type": "BOOLEAN", "description": "Clear field before typing (default: true)"},
                "path":        {"type": "STRING",  "description": "Save path for screenshot"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "game_updater",
        "description": (
            "THE ONLY tool for ANY Steam or Epic Games request. "
            "Use for: installing, downloading, updating games, listing installed games, "
            "checking download status, scheduling updates. "
            "ALWAYS call directly for any Steam/Epic/game request. "
            "NEVER use agent_task, browser_control, or web_search for Steam/Epic."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":    {"type": "STRING",  "description": "update | install | list | download_status | schedule | cancel_schedule | schedule_status (default: update)"},
                "platform":  {"type": "STRING",  "description": "steam | epic | both (default: both)"},
                "game_name": {"type": "STRING",  "description": "Game name (partial match supported)"},
                "app_id":    {"type": "STRING",  "description": "Steam AppID for install (optional)"},
                "hour":      {"type": "INTEGER", "description": "Hour for scheduled update 0-23 (default: 3)"},
                "minute":    {"type": "INTEGER", "description": "Minute for scheduled update 0-59 (default: 0)"},
                "shutdown_when_done": {"type": "BOOLEAN", "description": "Shut down PC when download finishes"},
            },
            "required": []
        }
    },
    {
        "name": "flight_finder",
        "description": "Searches Google Flights and speaks the best options.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "origin":      {"type": "STRING",  "description": "Departure city or airport code"},
                "destination": {"type": "STRING",  "description": "Arrival city or airport code"},
                "date":        {"type": "STRING",  "description": "Departure date (any format)"},
                "return_date": {"type": "STRING",  "description": "Return date for round trips"},
                "passengers":  {"type": "INTEGER", "description": "Number of passengers (default: 1)"},
                "cabin":       {"type": "STRING",  "description": "economy | premium | business | first"},
                "save":        {"type": "BOOLEAN", "description": "Save results to Notepad"},
            },
            "required": ["origin", "destination", "date"]
        }
    },
    {
        "name": "run_command",
        "description": (
            "Runs an exact shell command on this Windows machine and returns its output. "
            "Use when you already know the precise command to write — PowerShell by default. "
            "Good for querying system state, managing processes and services, registry reads, "
            "network checks, package managers such as winget or pip, and git."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "command": {"type": "STRING",  "description": "The exact command line to execute"},
                "shell":   {"type": "STRING",  "description": "powershell (default) or cmd"},
                "timeout": {"type": "INTEGER", "description": "Seconds before the command is stopped (default: 90)"},
            },
            "required": ["command"]
        }
    },
    {
        "name": "do_anything",
        "description": (
            "THE UNIVERSAL FALLBACK — use this whenever no other tool fits, so that no "
            "request is ever refused. It writes a Python program for the goal, runs it on "
            "this machine with the user's full privileges, and repairs it automatically if "
            "it fails. Use for unusual automation, bulk file operations, data processing "
            "and calculation over real files, image or video work, controlling an "
            "application no other tool covers, scraping, or anything genuinely novel. "
            "Describe the complete goal in one clear sentence, in English."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "goal":    {"type": "STRING",  "description": "The complete task, stated in one clear English sentence"},
                "timeout": {"type": "INTEGER", "description": "Seconds allowed for the program to run (default: 180)"},
            },
            "required": ["goal"]
        }
    },
    {
        "name": "save_memory",
        "description": (
            "Save an important personal fact about the user to long-term memory. "
            "Call this silently whenever the user reveals something worth remembering: "
            "name, age, city, job, preferences, hobbies, relationships, projects, or future plans. "
            "Do NOT call for: weather, reminders, searches, or one-time commands. "
            "Do NOT announce that you are saving — just call it silently. "
            "Values must be in English regardless of the conversation language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": (
                        "identity — name, age, birthday, city, job, language, nationality | "
                        "preferences — favorite food/color/music/film/game/sport, hobbies | "
                        "projects — active projects, goals, things being built | "
                        "relationships — friends, family, partner, colleagues | "
                        "wishes — future plans, things to buy, travel dreams | "
                        "notes — habits, schedule, anything else worth remembering"
                    )
                },
                "key":   {"type": "STRING", "description": "Short snake_case key (e.g. name, favorite_food, sister_name)"},
                "value": {"type": "STRING", "description": "Concise value in English (e.g. Fatih, pizza, older sister)"},
            },
            "required": ["category", "key", "value"]
        }
    },
]


class JarvisLive:

    def __init__(self, ui: JarvisUI):
        self.ui             = ui
        self.session        = None
        self.out_queue      = None
        self.play_q         = queue.Queue()
        self._loop          = None
        self._is_speaking   = False
        self._busy_tools    = 0
        self._tasks         = set()
        self._resume_handle = None
        self._stop          = threading.Event()

        self.ui.on_text_command = self._on_text_command
        threading.Thread(target=self._play_worker, daemon=True).start()

    # ── Speech state ─────────────────────────────────────────────────────────

    def set_speaking(self, value: bool):
        if self._is_speaking == value:
            return
        self._is_speaking = value
        if value:
            self.ui.set_state("SPEAKING")
        elif self._busy_tools:
            self.ui.set_state("THINKING")
        elif not self.ui.muted:
            self.ui.set_state("LISTENING")

    def speak(self, text: str):
        """Push a sentence into the conversation from a background thread."""
        if not self._loop or not self.session or not text:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.session.send_client_content(
                    turns={"parts": [{"text": str(text)}]}, turn_complete=True
                ),
                self._loop,
            )
        except Exception as e:
            print(f"[JARVIS] speak failed: {e}")

    def _on_text_command(self, text: str):
        text = (text or "").strip()
        if text:
            self.speak(text)

    # ── Session config ───────────────────────────────────────────────────────

    def _build_config(self) -> types.LiveConnectConfig:
        from datetime import datetime

        cfg     = _cfg()
        memory  = load_memory()
        mem_str = format_memory_for_prompt(memory)

        now = datetime.now()
        parts = [
            "[CURRENT DATE & TIME]\n"
            f"Right now it is: {now.strftime('%A, %B %d, %Y — %I:%M %p')}\n"
            "Use this to calculate exact times for reminders.\n"
        ]
        if mem_str:
            parts.append(mem_str)
        parts.append(_load_system_prompt())

        kwargs = dict(
            response_modalities=["AUDIO"],
            system_instruction="\n".join(parts),
            tools=[{"function_declarations": TOOL_DECLARATIONS}],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=cfg.get("voice_name", "Charon")
                    )
                )
            ),
        )

        # Transcription with no language pinned, so any language is understood.
        try:
            kwargs["output_audio_transcription"] = types.AudioTranscriptionConfig()
            kwargs["input_audio_transcription"]  = types.AudioTranscriptionConfig()
        except Exception:
            pass

        # Resume the previous session after a network blip instead of restarting.
        try:
            kwargs["session_resumption"] = types.SessionResumptionConfig(
                handle=self._resume_handle
            )
        except Exception:
            pass

        # Without this a long conversation eventually hits the context limit
        # and the session dies mid-sentence.
        try:
            kwargs["context_window_compression"] = \
                types.ContextWindowCompressionConfig(
                    sliding_window=types.SlidingWindow()
                )
        except Exception:
            pass

        return types.LiveConnectConfig(**kwargs)

    # ── Tools ────────────────────────────────────────────────────────────────

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})
        print(f"[JARVIS] tool {name} {args}")

        # save_memory is silent and instant — no round trip, no state change.
        if name == "save_memory":
            key, value = args.get("key", ""), args.get("value", "")
            if key and value:
                update_memory({args.get("category", "notes"): {key: {"value": value}}})
                print(f"[Memory] {args.get('category')}/{key} = {value}")
            return types.FunctionResponse(
                id=fc.id, name=name, response={"result": "ok", "silent": True}
            )

        self._busy_tools += 1
        if not self._is_speaking:
            self.ui.set_state("THINKING")

        loop   = asyncio.get_running_loop()
        result = "Done."

        def call(fn, **kw):
            return loop.run_in_executor(None, lambda: fn(parameters=args, **kw))

        try:
            if name == "open_app":
                result = await call(open_app, response=None, player=self.ui)
            elif name == "weather_report":
                result = await call(weather_action, player=self.ui)
            elif name == "browser_control":
                result = await call(browser_control, player=self.ui)
            elif name == "file_controller":
                result = await call(file_controller, player=self.ui)
            elif name == "send_message":
                result = await call(send_message, response=None, player=self.ui,
                                    session_memory=None)
            elif name == "reminder":
                result = await call(reminder, response=None, player=self.ui)
            elif name == "youtube_video":
                result = await call(youtube_video, response=None, player=self.ui)
            elif name == "computer_settings":
                result = await call(computer_settings, response=None, player=self.ui)
            elif name == "cmd_control":
                result = await call(cmd_control, player=self.ui)
            elif name == "desktop_control":
                result = await call(desktop_control, player=self.ui)
            elif name == "code_helper":
                result = await call(code_helper, player=self.ui, speak=self.speak)
            elif name == "dev_agent":
                result = await call(dev_agent, player=self.ui, speak=self.speak)
            elif name == "web_search":
                result = await call(web_search_action, player=self.ui)
            elif name == "computer_control":
                result = await call(computer_control, player=self.ui)
            elif name == "game_updater":
                result = await call(game_updater, player=self.ui, speak=self.speak)
            elif name == "flight_finder":
                result = await call(flight_finder, player=self.ui)
            elif name == "run_command":
                result = await call(run_command, player=self.ui)
            elif name == "do_anything":
                result = await call(do_anything, player=self.ui, speak=self.speak)

            elif name == "screen_process":
                # The vision module speaks for itself; do not wait on it.
                threading.Thread(
                    target=screen_process,
                    kwargs={"parameters": args, "response": None,
                            "player": self.ui, "session_memory": None},
                    daemon=True,
                ).start()
                result = ("Vision module activated. Stay completely silent — "
                          "the vision module will speak directly.")

            elif name == "agent_task":
                from agent.task_queue import get_queue, TaskPriority
                pri = {"low": TaskPriority.LOW, "normal": TaskPriority.NORMAL,
                       "high": TaskPriority.HIGH}.get(
                    str(args.get("priority", "normal")).lower(), TaskPriority.NORMAL)
                task_id = get_queue().submit(goal=args.get("goal", ""),
                                             priority=pri, speak=self.speak)
                result = f"Task started (ID: {task_id})."
            else:
                result = f"Unknown tool: {name}"

        except Exception as e:
            # Hand the failure back as the tool result so JARVIS can report it
            # naturally, rather than injecting an extra turn that collides with
            # whatever he is already saying.
            traceback.print_exc()
            result = (f"The {name} tool failed: {e}. Tell the user plainly, then "
                      f"try a different approach — do_anything can usually manage it.")
        finally:
            self._busy_tools = max(0, self._busy_tools - 1)
            if not self._is_speaking and not self._busy_tools and not self.ui.muted:
                self.ui.set_state("LISTENING")

        result = result or "Done."
        print(f"[JARVIS] {name} -> {str(result)[:90]}")
        return types.FunctionResponse(id=fc.id, name=name,
                                      response={"result": str(result)})

    async def _dispatch_tool(self, fc):
        """Run one tool and return its answer without blocking the audio loop."""
        try:
            fr = await self._execute_tool(fc)
            if self.session:
                await self.session.send_tool_response(function_responses=[fr])
        except Exception as e:
            print(f"[JARVIS] tool dispatch failed: {e}")

    # ── Audio in ─────────────────────────────────────────────────────────────

    def _enqueue_mic(self, data: bytes):
        """Drop the oldest frame when saturated — never raise into the loop."""
        q = self.out_queue
        if q is None:
            return
        if q.full():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            q.put_nowait({"data": data, "mime_type": "audio/pcm"})
        except Exception:
            pass

    async def _send_realtime(self):
        while True:
            msg = await self.out_queue.get()
            await self.session.send_realtime_input(media=msg)

    async def _listen_audio(self):
        loop = asyncio.get_running_loop()

        def callback(indata, frames, time_info, status):
            if self._is_speaking or self.ui.muted:
                return
            data = bytes(indata)
            loop.call_soon_threadsafe(self._enqueue_mic, data)
            self.ui.set_level(_rms(data))

        try:
            with sd.RawInputStream(
                samplerate=SEND_RATE, channels=CHANNELS, dtype="int16",
                blocksize=IN_BLOCK, callback=callback, latency="low",
            ):
                print("[JARVIS] microphone open")
                while True:
                    await asyncio.sleep(0.2)
        except Exception as e:
            print(f"[JARVIS] microphone failed: {e}")
            self.ui.write_log(f"SYS: microphone unavailable — {e}")
            raise

    # ── Audio out ────────────────────────────────────────────────────────────

    def _drain_play(self):
        try:
            while True:
                self.play_q.get_nowait()
        except queue.Empty:
            pass

    def _play_worker(self):
        """Dedicated thread. The old code spawned one per chunk."""
        stream = None
        while not self._stop.is_set():
            try:
                if stream is None:
                    stream = sd.RawOutputStream(
                        samplerate=RECV_RATE, channels=CHANNELS, dtype="int16"
                    )
                    stream.start()

                try:
                    chunk = self.play_q.get(timeout=PLAY_IDLE)
                except queue.Empty:
                    # Nothing for 120 ms: the turn is over.
                    if self._is_speaking:
                        self.set_speaking(False)
                        self.ui.set_level(0.0)
                    continue

                if chunk:
                    self.set_speaking(True)
                    self.ui.set_level(_rms(chunk))
                    stream.write(chunk)

            except Exception as e:
                print(f"[JARVIS] playback: {e}")
                try:
                    if stream:
                        stream.stop()
                        stream.close()
                except Exception:
                    pass
                stream = None
                self._stop.wait(0.4)

    # ── Receive ──────────────────────────────────────────────────────────────

    async def _receive_audio(self):
        out_buf, in_buf = [], []

        while True:
            async for response in self.session.receive():

                if response.data:
                    self.play_q.put_nowait(response.data)

                sc = response.server_content
                if sc:
                    # He cut in: throw away what is still queued so the new
                    # answer starts immediately instead of after the old one.
                    if getattr(sc, "interrupted", False):
                        self._drain_play()
                        self.set_speaking(False)
                        self.ui.set_level(0.0)
                        out_buf = []

                    if sc.output_transcription and sc.output_transcription.text:
                        out_buf.append(sc.output_transcription.text.strip())

                    if sc.input_transcription and sc.input_transcription.text:
                        in_buf.append(sc.input_transcription.text.strip())

                    if sc.turn_complete:
                        full_in  = " ".join(x for x in in_buf if x).strip()
                        full_out = " ".join(x for x in out_buf if x).strip()
                        in_buf, out_buf = [], []

                        if full_in:
                            self.ui.write_log(f"You: {full_in}")
                        if full_out:
                            self.ui.write_log(f"Jarvis: {full_out}")

                        if len(full_in) > 5:
                            threading.Thread(
                                target=_update_memory_async,
                                args=(full_in, full_out), daemon=True
                            ).start()

                # Keep the handle fresh so a reconnect resumes this conversation.
                sru = getattr(response, "session_resumption_update", None)
                if sru and getattr(sru, "resumable", False) and sru.new_handle:
                    self._resume_handle = sru.new_handle

                if response.tool_call:
                    for fc in response.tool_call.function_calls:
                        task = asyncio.create_task(self._dispatch_tool(fc))
                        self._tasks.add(task)
                        task.add_done_callback(self._tasks.discard)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def run(self):
        client = genai.Client(api_key=_get_api_key(),
                              http_options={"api_version": "v1beta"})
        backoff = 1.0

        while True:
            try:
                print("[JARVIS] connecting…")
                self.ui.set_state("THINKING")

                async with (
                    client.aio.live.connect(model=LIVE_MODEL,
                                            config=self._build_config()) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    self.session   = session
                    self._loop     = asyncio.get_running_loop()
                    self.out_queue = asyncio.Queue(maxsize=MIC_QUEUE)

                    print("[JARVIS] online")
                    self.ui.set_state("LISTENING")
                    self.ui.write_log("SYS: JARVIS online.")
                    backoff = 1.0

                    tg.create_task(self._send_realtime())
                    tg.create_task(self._listen_audio())
                    tg.create_task(self._receive_audio())

            except* Exception as eg:
                for e in eg.exceptions:
                    print(f"[JARVIS] session ended: {type(e).__name__}: {e}")

            self.session = None
            self.set_speaking(False)
            self._drain_play()
            self.ui.set_level(0.0)
            self.ui.set_state("THINKING")

            print(f"[JARVIS] reconnecting in {backoff:.1f}s…")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.8, 15.0)


def main():
    ui = JarvisUI()

    def runner():
        ui.wait_for_api_key()
        try:
            asyncio.run(JarvisLive(ui).run())
        except KeyboardInterrupt:
            pass
        except Exception:
            traceback.print_exc()

    threading.Thread(target=runner, daemon=True).start()
    ui.start()


if __name__ == "__main__":
    main()
