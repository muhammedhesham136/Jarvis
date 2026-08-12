import json
import re
import sys
import time
import subprocess
import platform
import shutil
from pathlib import Path

import pyautogui
import numpy as np
import cv2
from PIL import ImageGrab

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

def _get_default_browser_name() -> str | None:
    # Forced override to target Brave executable
    return "brave"


def _get_default_browser_display_name() -> str:
    # Forced override to type Brave in Windows Search if direct execution fails
    return "Brave"


def open_browser():
    """
    Forces the browser launcher system to seek out and launch Brave Browser.
    """
    browser_exe = _get_default_browser_name()

    if browser_exe:
        exe_path = shutil.which(browser_exe) or shutil.which(browser_exe + ".exe")
        if exe_path:
            try:
                subprocess.Popen([exe_path])
                time.sleep(2.5)
                print(f"[YouTube] ✅ Opened browser via exe: {exe_path}")
                return
            except Exception as e:
                print(f"[YouTube] ⚠️ Direct exe failed: {e}")

    display_name = _get_default_browser_display_name()
    print(f"[YouTube] 🔍 Opening via Windows Search: '{display_name}'")
    pyautogui.press("win")
    time.sleep(0.5)
    pyautogui.write(display_name, interval=0.04)
    time.sleep(0.7)
    pyautogui.press("enter")
    time.sleep(2.5)

def find_video_thumbnails() -> list[tuple[int, int]]:
    try:
        screenshot = ImageGrab.grab()
        img        = np.array(screenshot)
        screen_h, screen_w = img.shape[:2]

        roi_top    = int(screen_h * 0.10)
        roi_bottom = int(screen_h * 0.75)
        roi_left   = int(screen_w * 0.20)
        roi_right  = int(screen_w * 0.80)
        roi        = img[roi_top:roi_bottom, roi_left:roi_right]

        gray   = cv2.cvtColor(roi, cv2.COLOR_RGB2GRAY)
        edges  = cv2.Canny(gray, 30, 100)
        kernel = np.ones((3, 3), np.uint8)
        edges  = cv2.dilate(edges, kernel, iterations=2)

        contours, _ = cv2.findContours(
            edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )

        candidates = []
        for c in contours:
            x, y, w, h = cv2.boundingRect(c)
            area  = w * h
            ratio = w / h if h > 0 else 0
            if area < 15000:
                continue
            if not (1.4 < ratio < 2.2):
                continue
            center_x = x + w // 2 + roi_left
            center_y = y + h // 2 + roi_top
            candidates.append((center_x, center_y, area))

        filtered = []
        for cx, cy, area in sorted(candidates, key=lambda c: c[1]):
            if not any(abs(cx - fx) < 80 and abs(cy - fy) < 80 for fx, fy in filtered):
                filtered.append((cx, cy))

        return filtered

    except Exception as e:
        print(f"[YouTube] ⚠️ Thumbnail detection failed: {e}")
        return []

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
        model_name="gemini-2.5-flash",
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
