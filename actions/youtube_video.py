import json
import os
import re
import sys
import time
import subprocess
import platform
import shutil
import webbrowser
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    _TRANSCRIPT_OK = True
except ImportError:
    _TRANSCRIPT_OK = False


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]

def _get_brave_executable() -> str | None:
    """Brave is not on PATH, so shutil.which alone never finds it."""
    if platform.system() != "Windows":
        return shutil.which("brave-browser") or shutil.which("brave")

    try:
        import winreg
        key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\brave.exe"
        for hive in [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]:
            try:
                key = winreg.OpenKey(hive, key_path)
                val = winreg.QueryValue(key, None)
                winreg.CloseKey(key)
                exe = val.strip().strip('"')
                if exe and Path(exe).exists():
                    return exe
            except Exception:
                continue
    except Exception:
        pass

    for root in [os.environ.get("PROGRAMFILES"),
                 os.environ.get("PROGRAMFILES(X86)"),
                 os.environ.get("LOCALAPPDATA")]:
        if not root:
            continue
        candidate = Path(root) / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe"
        if candidate.exists():
            return str(candidate)

    return shutil.which("brave") or shutil.which("brave.exe")


def open_url(url: str) -> bool:
    """
    Opens the url in a single window and reports whether the launch worked.
    Brave is tried first because the OS default handler is not necessarily Brave.
    """
    exe_path = _get_brave_executable()
    if exe_path:
        try:
            subprocess.Popen([exe_path, url])
            print(f"[YouTube] ✅ Opened via exe: {exe_path}")
            return True
        except Exception as e:
            print(f"[YouTube] ⚠️ Direct exe failed: {e}")

    try:
        if webbrowser.open(url):
            print("[YouTube] ✅ Opened via default browser handler")
            return True
    except Exception as e:
        print(f"[YouTube] ⚠️ Default browser launch failed: {e}")

    return False


def _resolve_top_video(query: str) -> tuple[str | None, str | None]:
    """Resolves the first search hit server-side so playback needs no mouse automation."""
    if not (_REQUESTS_OK and query):
        return None, None

    try:
        resp = requests.get(
            f"https://www.youtube.com/results?search_query={requests.utils.quote(query)}",
            headers=HEADERS, timeout=10
        )
        resp.raise_for_status()

        vid = re.search(r'"videoId":"([A-Za-z0-9_-]{11})"', resp.text)
        if not vid:
            return None, None

        title = re.search(
            r'"title":\{"runs":\[\{"text":"((?:[^"\\]|\\.)*)"',
            resp.text[vid.start():]
        )
        if not title:
            return vid.group(1), None

        return vid.group(1), json.loads(f'"{title.group(1)}"')

    except Exception as e:
        print(f"[YouTube] ⚠️ Search resolution failed: {e}")
        return None, None


def _extract_video_id(url: str) -> str | None:
    patterns = [r"(?:v=|\/v\/|youtu\.be\/|\/embed\/|\/shorts\/)([A-Za-z0-9_-]{11})"]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def _is_valid_youtube_url(url: str) -> bool:
    return bool(re.search(r"(youtube\.com|youtu\.be)", url or ""))


def _ask_for_url(prompt_text: str = "YouTube video URL:") -> str | None:
    try:
        import tkinter as tk
        from tkinter import simpledialog

        root = tk._default_root
        if root is None:
            root = tk.Tk()
            root.withdraw()

        url = simpledialog.askstring("J.A.R.V.I.S", prompt_text, parent=root)
        return url.strip() if url else None
    except Exception as e:
        print(f"[YouTube] ⚠️ URL dialog failed: {e}")
        return None


def _get_transcript(video_id: str) -> str | None:
    if not _TRANSCRIPT_OK:
        return None

    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
        transcript = None

        try:
            transcript = transcript_list.find_manually_created_transcript(
                ["en", "tr", "de", "fr", "es", "it", "pt", "ru", "ja", "ko", "ar", "zh"]
            )
        except Exception:
            pass

        if transcript is None:
            try:
                transcript = transcript_list.find_generated_transcript(
                    ["en", "tr", "de", "fr", "es", "it", "pt", "ru", "ja", "ko", "ar", "zh"]
                )
            except Exception:
                for t in transcript_list:
                    transcript = t
                    break

        if transcript is None:
            return None

        fetched = transcript.fetch()
        text    = " ".join(entry["text"] for entry in fetched)
        return text

    except Exception as e:
        print(f"[YouTube] ⚠️ Transcript fetch failed: {e}")
        return None


def _summarize_with_gemini(transcript: str, video_url: str) -> str:
    from core import genai_compat as genai

    genai.configure(api_key=_get_api_key())
    model = genai.GenerativeModel(
        model_name="gemini-3.5-flash",
        system_instruction=(
            "You are JARVIS, Tony Stark's AI assistant. "
            "Summarize YouTube video transcripts clearly and concisely. "
            "Structure: 1-sentence overview, then 3-5 key points. "
            "Be direct. Address the user as 'sir'. "
            "Match the language of the transcript."
        )
    )

    max_chars = 80000
    truncated = transcript[:max_chars] + ("..." if len(transcript) > max_chars else "")

    response = model.generate_content(
        f"Please summarize this YouTube video transcript:\n\nUrl: {video_url}\nTranscript:\n{truncated}"
    )
    return response.text.strip()


def youtube_video(parameters: dict, response=None, player=None, session_memory=None) -> str:
    """Master tool endpoint for YouTube handling."""
    action = (parameters or {}).get("action", "play").strip().lower()
    query = (parameters or {}).get("query", "").strip()
    url = (parameters or {}).get("url", "").strip()
    save_to_file = (parameters or {}).get("save", False)

    if action == "play":
        open_browser()
        # Open YouTube main search string
        search_url = "https://youtube.com"
        if query:
            search_url = f"https://youtube.com/results?search_query={requests.utils.quote(query)}" if _REQUESTS_OK else f"https://youtube.com/results?search_query={query.replace(' ', '+')}"
        
        # Windows command to fallback open URL in default browser context if automation fails
        import webbrowser
        webbrowser.open(search_url)
        
        if query:
            time.sleep(4.0)
            thumbnails = find_video_thumbnails()
            if thumbnails:
                pyautogui.click(thumbnails[0][0], thumbnails[0][1])
                return f"Playing the top result for '{query}' on YouTube, sir."
        return "Opened YouTube for you, sir."

    if action == "summarize" or action == "get_info":
        if not url:
            url = _ask_for_url()
        if not url or not _is_valid_youtube_url(url):
            return "Invalid or missing YouTube URL, sir."

        vid_id = _extract_video_id(url)
        if not vid_id:
            return "Could not extract video ID from the URL, sir."

        transcript = _get_transcript(vid_id)
        if not transcript:
            return "Could not fetch a transcript for this video, sir."

        summary = _summarize_with_gemini(transcript, url)
        
        if save_to_file:
            desktop = Path.home() / "Desktop"
            out_file = desktop / f"YouTube_Summary_{vid_id}.txt"
            out_file.write_text(summary, encoding="utf-8")
            subprocess.Popen(["notepad.exe", str(out_file)])
            return f"Summary saved to your desktop, sir."
            
        return summary

    return f"Action '{action}' is not supported yet on YouTube modules, sir."
