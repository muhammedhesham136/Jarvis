"""
J.A.R.V.I.S — holographic core.

Fullscreen, pure black, nothing on screen but the reactor. State is never
written out as text; it is carried entirely by colour and motion:

    LISTENING   cool cyan, slow breathing
    SPEAKING    bright white-cyan, spectrum driven by the live audio
    THINKING    amber, faster rotation
    MUTED       deep red, almost still

Performance
-----------
Every canvas item is created once during start-up and afterwards only
updated through ``coords()`` / ``itemconfig()``. Nothing is deleted or
rebuilt per frame. The assistant's audio runs on other threads and must
never be starved by the Tk event loop.

Thread safety
-------------
Actions call ``write_log`` from worker threads and Tk is not thread safe,
so those calls only append to a deque. The animation tick — which runs on
the Tk thread — drains it.
"""

import json
import math
import os
import sys
import threading
import time
from collections import deque
from pathlib import Path

import tkinter as tk
from PIL import Image, ImageDraw, ImageFilter, ImageTk


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR   = get_base_dir()
CONFIG_DIR = BASE_DIR / "config"
API_FILE   = CONFIG_DIR / "api_keys.json"

# ── Look ──────────────────────────────────────────────────────────────────────
C_BG = "#000000"

PALETTE = {
    "LISTENING":    (0x30, 0xC6, 0xFF),
    "SPEAKING":     (0x9C, 0xEA, 0xFF),
    "THINKING":     (0xFF, 0xA6, 0x2C),
    "PROCESSING":   (0xFF, 0xA6, 0x2C),
    "MUTED":        (0xFF, 0x2E, 0x55),
    "INITIALISING": (0x1E, 0x5A, 0x72),
}

FPS         = 45
FRAME_MS    = max(1, 1000 // FPS)
N_BARS      = 88          # spectrum ring
N_TICKS     = 72          # outer graduation ring
GLOW_LEVELS = 14          # pre-rendered core brightness steps
LOG_LINES   = 300


class JarvisUI:

    # ── Construction ──────────────────────────────────────────────────────────

    def __init__(self, face_path=None, size=None):
        self.root = tk.Tk()
        self.root.title("J.A.R.V.I.S")
        self.root.configure(bg=C_BG)
        self.root.attributes("-fullscreen", True)

        self.W = self.root.winfo_screenwidth()
        self.H = self.root.winfo_screenheight()

        # Diameter of the whole HUD, and its radius.
        self.S  = min(self.H * 0.74, 780)
        self.R  = self.S / 2
        self.CX = self.W // 2
        self.CY = self.H // 2

        # ── Public state (main.py reads these) ────────────────────────────────
        self.muted           = False
        self.speaking        = False
        self.on_text_command = None

        # ── Internal state ────────────────────────────────────────────────────
        self._state      = "INITIALISING"
        self._t          = 0
        self._level      = 0.0     # smoothed audio energy, 0..1
        self._level_raw  = 0.0     # written by other threads
        self._breath     = 0.0
        self._rgb        = list(PALETTE["INITIALISING"])
        self._last_shade = None

        self._log_queue = deque()   # thread-safe hand-off to the Tk thread
        self._log_lines = deque(maxlen=LOG_LINES)

        self._spin = [0.0, 140.0, 250.0, 0.0]
        self._glow_cache = {}

        self._build_canvas()
        self._build_geometry()
        self._build_input()
        self._bind_keys()

        self._api_key_ready = API_FILE.exists()
        if not self._api_key_ready:
            self._show_setup()

        self._tick()
        self.root.protocol("WM_DELETE_WINDOW", lambda: os._exit(0))

    # ── Canvas & pre-rendered art ─────────────────────────────────────────────

    def _build_canvas(self):
        self.c = tk.Canvas(self.root, width=self.W, height=self.H,
                           bg=C_BG, highlightthickness=0, bd=0)
        self.c.place(x=0, y=0)

        # A single greyscale falloff, tinted lazily per palette. Building it
        # once keeps start-up instant and per-frame cost at zero.
        G = int(self.S * 0.44)
        mask = Image.new("L", (G, G), 0)
        d    = ImageDraw.Draw(mask)
        for i in range(90, 0, -1):
            f = i / 90.0
            r = (G / 2) * f
            v = int(255 * (1.0 - f) ** 2.0)
            d.ellipse((G / 2 - r, G / 2 - r, G / 2 + r, G / 2 + r), fill=v)
        self._glow_mask = mask.filter(ImageFilter.GaussianBlur(G / 34.0))

    def _glow_set(self, rgb):
        """Pre-rendered brightness ramp for one palette colour, cached."""
        key = tuple(rgb)
        cached = self._glow_cache.get(key)
        if cached:
            return cached

        black = Image.new("RGB", self._glow_mask.size, (0, 0, 0))
        tint  = Image.new("RGB", self._glow_mask.size, key)
        out   = []
        for i in range(GLOW_LEVELS):
            f = (i + 1) / GLOW_LEVELS
            a = self._glow_mask.point(lambda v, f=f: int(v * f))
            out.append(ImageTk.PhotoImage(Image.composite(tint, black, a)))
        self._glow_cache[key] = out
        return out

    @staticmethod
    def _shade(rgb, f):
        f = max(0.0, min(1.0, f))
        return "#%02x%02x%02x" % (int(rgb[0] * f), int(rgb[1] * f), int(rgb[2] * f))

    def _build_geometry(self):
        c, R, CX, CY = self.c, self.R, self.CX, self.CY

        # Spectrum ring — stationary, so the trig is computed once.
        self._bar_in  = R * 0.44
        self._bar_len = R * 0.27
        self._bar_cos, self._bar_sin, self._bar_ids = [], [], []
        for i in range(N_BARS):
            a = (i / N_BARS) * 2 * math.pi - math.pi / 2
            self._bar_cos.append(math.cos(a))
            self._bar_sin.append(math.sin(a))
            self._bar_ids.append(c.create_line(CX, CY, CX, CY, width=2, fill=C_BG))

        self._bar_val   = [0.0] * N_BARS
        self._bar_phase = [(i * 2.39996323) % (2 * math.pi) for i in range(N_BARS)]

        # Rotating arc rings: (radius fraction, stroke, segments, arc length).
        self._ring_ids = []
        for r_frac, w, segs, ext in ((0.76, 3, 3, 88), (0.85, 2, 6, 38), (0.93, 1, 2, 150)):
            r   = R * r_frac
            box = (CX - r, CY - r, CX + r, CY + r)
            self._ring_ids.append(
                [c.create_arc(*box, start=0, extent=ext, style="arc",
                              width=w, outline=C_BG) for _ in range(segs)]
            )

        # Outer graduation ticks — slow counter-rotation.
        self._tick_out = R * 0.99
        self._tick_ids = [c.create_line(CX, CY, CX, CY, width=1, fill=C_BG)
                          for _ in range(N_TICKS)]

        # Sweep, core glow, core disc.
        r = R * 0.85
        self._sweep_id = c.create_arc(CX - r, CY - r, CX + r, CY + r,
                                      start=0, extent=26, style="arc",
                                      width=2, outline=C_BG)
        self._glow_id  = c.create_image(CX, CY, image=None)
        self._core_id  = c.create_oval(CX, CY, CX, CY, outline=C_BG, width=2, fill="")

    # ── Command bar (hidden until you type) ───────────────────────────────────

    def _build_input(self):
        self._entry_visible = False
        self._entry_var = tk.StringVar()
        self._entry = tk.Entry(
            self.root, textvariable=self._entry_var,
            fg="#bdf3ff", bg="#000508", insertbackground="#30c6ff",
            borderwidth=0, highlightthickness=1,
            highlightbackground="#0d3a4a", highlightcolor="#30c6ff",
            font=("Consolas", 13), justify="center",
        )
        self._entry.bind("<Return>", self._submit)
        self._entry.bind("<Escape>", lambda e: self._hide_entry())

    def _show_entry(self, seed=""):
        if self._entry_visible:
            return
        w = int(self.W * 0.42)
        self._entry.place(x=(self.W - w) // 2, y=int(self.H * 0.86),
                          width=w, height=32)
        self._entry_visible = True
        self._entry_var.set(seed)
        self._entry.icursor(tk.END)
        self._entry.focus_set()

    def _hide_entry(self):
        if not self._entry_visible:
            return
        self._entry.place_forget()
        self._entry_visible = False
        self._entry_var.set("")
        self.root.focus_set()

    def _submit(self, _event=None):
        text = self._entry_var.get().strip()
        self._hide_entry()
        if not text:
            return
        self.write_log(f"You: {text}")
        if self.on_text_command:
            threading.Thread(target=self.on_text_command,
                             args=(text,), daemon=True).start()

    # ── Keys ──────────────────────────────────────────────────────────────────

    def _bind_keys(self):
        r = self.root
        r.bind("<F4>", lambda e: self.toggle_mute())
        r.bind("<F11>", lambda e: r.attributes(
            "-fullscreen", not r.attributes("-fullscreen")))
        r.bind("<Control-q>", lambda e: os._exit(0))
        r.bind("<Escape>", lambda e: self._hide_entry())
        r.bind("<Key>", self._on_key)

    def _on_key(self, event):
        if self._entry_visible or not self._api_key_ready:
            return
        ch = event.char
        if ch and ch.isprintable() and ch not in ("\r", "\n"):
            self._show_entry(seed=ch)

    # ── Public API used by main.py and the action modules ─────────────────────

    def set_state(self, state: str):
        """LISTENING | SPEAKING | THINKING | PROCESSING | MUTED"""
        if self.muted and state != "MUTED":
            state = "MUTED"
        self._state   = state if state in PALETTE else "LISTENING"
        self.speaking = (state == "SPEAKING")

    def set_level(self, level: float):
        """Live audio energy, 0..1. Safe to call from any thread."""
        try:
            self._level_raw = max(0.0, min(1.0, float(level)))
        except (TypeError, ValueError):
            pass

    def write_log(self, text: str):
        """Safe to call from any thread — the Tk tick drains this."""
        self._log_queue.append(str(text))

    def toggle_mute(self):
        self.muted = not self.muted
        if self.muted:
            self.set_state("MUTED")
            self.write_log("SYS: Microphone muted.")
        else:
            self._state = "LISTENING"
            self.write_log("SYS: Microphone live.")

    def start_speaking(self):
        self.set_state("SPEAKING")

    def stop_speaking(self):
        if not self.muted:
            self.set_state("LISTENING")

    def wait_for_api_key(self):
        while not self._api_key_ready:
            time.sleep(0.05)

    def start(self):
        """Blocks until the window closes. Must run on the main thread."""
        self.root.mainloop()

    # ── Animation ─────────────────────────────────────────────────────────────

    def _tick(self):
        self._t += 1
        t = self._t

        while self._log_queue:
            line = self._log_queue.popleft()
            self._log_lines.append(line)
            print(f"[UI] {line}")

        # Energy: real audio while speaking, a gentle idle shimmer otherwise.
        speaking = self.speaking
        thinking = self._state in ("THINKING", "PROCESSING")
        if self.muted:
            target = 0.0
        elif speaking:
            target = self._level_raw
        elif thinking:
            target = 0.20 + 0.10 * math.sin(t * 0.13)
        else:
            target = 0.07 + 0.03 * math.sin(t * 0.045)
        self._level += (target - self._level) * (0.42 if speaking else 0.10)

        self._breath += ((1.0 if speaking else 0.55) - self._breath) * 0.06

        # Colour eases toward the current state's palette.
        want = PALETTE.get(self._state, PALETTE["LISTENING"])
        for i in range(3):
            self._rgb[i] += (want[i] - self._rgb[i]) * 0.09
        rgb = [int(v) for v in self._rgb]

        speed = 2.1 if speaking else (1.5 if thinking else 0.75)
        self._spin[0] = (self._spin[0] + 0.55 * speed) % 360
        self._spin[1] = (self._spin[1] - 0.34 * speed) % 360
        self._spin[2] = (self._spin[2] + 0.20 * speed) % 360
        self._spin[3] = (self._spin[3] + 1.70 * speed) % 360

        self._draw_bars(rgb)
        self._draw_rings(rgb)
        self._draw_ticks(rgb)
        self._draw_core(rgb)
        self._last_shade = rgb

        self.root.after(FRAME_MS, self._tick)

    def _recolour(self) -> bool:
        """True when the palette moved enough to be worth re-writing colours."""
        if self._last_shade is None:
            return True
        return any(abs(a - b) >= 3 for a, b in zip(self._rgb, self._last_shade))

    def _draw_bars(self, rgb):
        c   = self.c
        CX, CY = self.CX, self.CY
        r_in, blen = self._bar_in, self._bar_len
        lvl = self._level
        t   = self._t
        recolour = self._recolour()
        col_hi = self._shade(rgb, 1.0)
        col_lo = self._shade(rgb, 0.42)

        for i in range(N_BARS):
            wobble = 0.55 + 0.45 * math.sin(t * 0.10 + self._bar_phase[i] * 2.7)
            target = 0.05 + lvl * wobble
            v = self._bar_val[i] + (target - self._bar_val[i]) * 0.34
            self._bar_val[i] = v

            length = blen * min(1.0, v)
            cs, sn = self._bar_cos[i], self._bar_sin[i]
            c.coords(self._bar_ids[i],
                     CX + r_in * cs, CY + r_in * sn,
                     CX + (r_in + length) * cs, CY + (r_in + length) * sn)
            if recolour or True:
                c.itemconfig(self._bar_ids[i],
                             fill=col_hi if v > 0.42 else col_lo)

    def _draw_rings(self, rgb):
        c = self.c
        for idx, ids in enumerate(self._ring_ids):
            base = self._spin[idx]
            step = 360.0 / len(ids)
            col  = self._shade(rgb, 0.82 - idx * 0.16)
            for k, item in enumerate(ids):
                c.itemconfig(item, start=(base + k * step) % 360, outline=col)

        c.itemconfig(self._sweep_id, start=self._spin[3],
                     outline=self._shade(rgb, 1.0))

    def _draw_ticks(self, rgb):
        c = self.c
        CX, CY = self.CX, self.CY
        out = self._tick_out
        rot = math.radians(-self._spin[2] * 0.5)
        col_major = self._shade(rgb, 0.60)
        col_minor = self._shade(rgb, 0.26)

        for i in range(N_TICKS):
            a  = rot + (i / N_TICKS) * 2 * math.pi
            cs, sn = math.cos(a), math.sin(a)
            major = (i % 6 == 0)
            inn = out - (self.R * 0.055 if major else self.R * 0.022)
            c.coords(self._tick_ids[i],
                     CX + out * cs, CY + out * sn,
                     CX + inn * cs, CY + inn * sn)
            c.itemconfig(self._tick_ids[i],
                         fill=col_major if major else col_minor)

    def _draw_core(self, rgb):
        c = self.c
        pulse = self._breath * (0.55 + 0.45 * self._level)
        idx   = int(min(GLOW_LEVELS - 1, max(0, pulse * (GLOW_LEVELS - 1))))
        c.itemconfig(self._glow_id, image=self._glow_set(rgb)[idx])

        r = self.R * (0.155 + 0.030 * self._level + 0.012 * math.sin(self._t * 0.06))
        c.coords(self._core_id,
                 self.CX - r, self.CY - r, self.CX + r, self.CY + r)
        c.itemconfig(self._core_id, outline=self._shade(rgb, 0.9 + 0.1 * self._level))

    # ── First-run API key ─────────────────────────────────────────────────────

    def _show_setup(self):
        self._setup = tk.Frame(self.root, bg="#00060a",
                               highlightbackground="#30c6ff", highlightthickness=1)
        self._setup.place(relx=0.5, rely=0.5, anchor="center")

        tk.Label(self._setup, text="J . A . R . V . I . S",
                 fg="#30c6ff", bg="#00060a",
                 font=("Consolas", 17, "bold")).pack(padx=48, pady=(26, 4))
        tk.Label(self._setup, text="Gemini API key required to come online",
                 fg="#2b6d84", bg="#00060a",
                 font=("Consolas", 9)).pack(pady=(0, 16))

        self._key_entry = tk.Entry(self._setup, width=50, show="*",
                                   fg="#bdf3ff", bg="#000508",
                                   insertbackground="#30c6ff", borderwidth=0,
                                   highlightthickness=1,
                                   highlightbackground="#0d3a4a",
                                   highlightcolor="#30c6ff",
                                   font=("Consolas", 11), justify="center")
        self._key_entry.pack(ipady=5)
        self._key_entry.bind("<Return>", lambda e: self._save_key())
        self._key_entry.focus_set()

        tk.Button(self._setup, text="INITIALISE", command=self._save_key,
                  fg="#30c6ff", bg="#00060a", activebackground="#062633",
                  activeforeground="#bdf3ff", borderwidth=0,
                  font=("Consolas", 10, "bold"), cursor="hand2",
                  pady=7).pack(pady=(16, 26))

    def _save_key(self):
        key = self._key_entry.get().strip()
        if not key:
            return
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = {}
        if API_FILE.exists():
            try:
                data = json.loads(API_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data["gemini_api_key"] = key
        data.setdefault("model_name", "gemini-2.5-flash")
        API_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")

        self._setup.destroy()
        self._api_key_ready = True
        self.root.focus_set()
        self.set_state("LISTENING")
