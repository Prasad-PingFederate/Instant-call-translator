"""
Instant Call Translator - Professional Edition
Real-Time Speech-to-Telugu Subtitle HUD for Online Client Calls & In-Person Room Meetings.
Equipped with:
- Spoken English Accent Selector (US, UK, India, Australia)
- Dynamic Font Size Scaling (A+ / A-)
- 0ms Latency LRU Translation Cache
- One-Click Pause / Resume Toggle
- Adaptive Noise Floor Calibration & Far-Field Room Gain
- IT & Business Terminology Preserver
- Seamless Bluetooth / Headphone Reconnect Resilience
"""

import os
import sys
import time
import math
import queue
import logging
import warnings
import threading
from functools import lru_cache
import tkinter as tk
from tkinter import ttk, messagebox
import requests
import numpy as np

# Audio & Speech Recognition
import soundcard as sc
import speech_recognition as sr

# Suppress soundcard buffer warnings
warnings.filterwarnings("ignore")

# Performance & Audio Constants
DEFAULT_SAMPLE_RATE = 16000
CHUNK_DURATION_SEC = 0.06  # 60ms chunks for rapid VAD response
MIN_SPEECH_DURATION_SEC = 0.35  # Ignore transient clicks under 350ms
MAX_SPEECH_DURATION_SEC = 7.5  # Max sentence chunk to prevent long translation delays

SUPPORTED_LANGUAGES = {
    "Telugu (తెలుగు)": "te",
    "Hindi (हिन्दी)": "hi",
    "Tamil (தமிழ்)": "ta",
    "Kannada (ಕನ್ನಡ)": "kn",
    "Marathi (मराठी)": "mr",
    "Malayalam (മലയാളം)": "ml",
    "Gujarati (ગુજરાતી)": "gu",
    "Bengali (বাংলা)": "bn",
    "English (Original)": "en",
}

CLIENT_ACCENTS = {
    "🇺🇸 US Client (en-US)": "en-US",
    "🇮🇳 Indian English (en-IN)": "en-IN",
    "🇬🇧 UK Client (en-GB)": "en-GB",
    "🇦🇺 Australia (en-AU)": "en-AU",
}

# Mode Profiles
MODE_PROFILES = {
    "online": {
        "name": "🎧 Online Call (Teams/Zoom)",
        "use_loopback": True,
        "sensitivity": 0.012,
        "silence_sec": 0.50,
        "gain": 1.0,
        "desc": "Captures client voice from speakers/headphones",
    },
    "offline_room": {
        "name": "🎙️ Offline Meeting (Room Mic)",
        "use_loopback": False,
        "sensitivity": 0.006,  # High sensitivity for distant table voices
        "silence_sec": 0.45,  # Rapid finalization for instant translation
        "gain": 2.2,  # Far-field boost for speakers across the room
        "desc": "High-gain room mic for in-person conference meetings",
    },
}

# IT & Client Terminology Glossary to preserve natural tech terms in Telugu
IT_GLOSSARY_PRESERVE = {
    "PR": "PR (పుల్ రిక్వెస్ట్)",
    "pull request": "పుల్ రిక్వెస్ట్ (PR)",
    "sprint": "స్ప్రింట్ (Sprint)",
    "backlog": "బ్యాక్‌లాగ్ (Backlog)",
    "blocker": "బ్లాకర్ (Blocker)",
    "blockers": "బ్లాకర్స్ (Blockers)",
    "deployment": "డిప్లాయ్‌మెంట్ (Deployment)",
    "deploy": "డిప్లాయ్ (Deploy)",
    "staging": "స్టేజింగ్ (Staging)",
    "production": "ప్రొడక్షన్ (Production)",
    "timeline": "టైమ్‌లైన్ (Timeline)",
    "deadline": "డెడ్‌లైన్ (Deadline)",
    "deliverable": "డెలివరబుల్ (Deliverable)",
    "deliverables": "డెలివరబుల్స్ (Deliverables)",
    "pipeline": "పైప్‌లైన్ (Pipeline)",
    "standup": "స్టాండప్ (Standup)",
    "sync": "సింక్ (Sync)",
    "walkthrough": "వాక్‌త్రూ (Walkthrough)",
    "bug": "బగ్ (Bug)",
    "bugs": "బగ్స్ (Bugs)",
    "release": "రిలీజ్ (Release)",
}


def clean_translated_text(text: str, target_lang: str) -> str:
    """Polishes machine-translated text with IT domain terminology context."""
    if not text or target_lang != "te":
        return text

    replacements = {
        "విస్తరణ": "డిప్లాయ్‌మెంట్ (Deployment)",
        "పుల్ అభ్యర్థన": "పుల్ రిక్వెస్ట్ (PR)",
        "లాగడం అభ్యర్థన": "పుల్ రిక్వెస్ట్ (PR)",
        "అడ్డంకి": "బ్లాకర్ (Blocker)",
        "అడ్డంకులు": "బ్లాకర్స్ (Blockers)",
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    return text


@lru_cache(maxsize=1024)
def translate_text(text: str, target_lang: str = "te") -> str:
    """Translates text with LRU caching and dual-engine failover."""
    cleaned_input = text.strip() if text else ""
    if not cleaned_input:
        return ""
    if target_lang == "en":
        return cleaned_input

    # Engine 1: Google High-Speed Engine (~150-300ms latency)
    try:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {
            "client": "gtx",
            "sl": "en",
            "tl": target_lang,
            "dt": "t",
            "q": cleaned_input,
        }
        res = requests.get(url, params=params, timeout=3.5)
        if res.status_code == 200:
            data = res.json()
            translated = "".join(part[0] for part in data[0] if part[0])
            if translated:
                return clean_translated_text(translated, target_lang)
    except Exception:
        pass

    # Engine 2: Fallback Engine via MyMemory
    try:
        from deep_translator import MyMemoryTranslator

        lang_code_map = {"te": "te-IN", "hi": "hi-IN", "ta": "ta-IN", "kn": "kn-IN"}
        dest = lang_code_map.get(target_lang, target_lang)
        fallback_res = MyMemoryTranslator(source="en-GB", target=dest).translate(cleaned_input)
        if fallback_res:
            return clean_translated_text(fallback_res, target_lang)
    except Exception:
        pass

    return cleaned_input


class AudioCaptureWorker(threading.Thread):
    """Adaptive Audio Capture with Dynamic Noise Floor Calibration & Bluetooth resilience."""

    def __init__(self, audio_queue, status_callback, level_callback):
        super().__init__(daemon=True)
        self.audio_queue = audio_queue
        self.status_callback = status_callback
        self.level_callback = level_callback
        self.running = False
        self.is_paused = False

        self.current_mode = "online"
        self._apply_profile(MODE_PROFILES[self.current_mode])
        self.mode_changed = False
        self.noise_floor = 0.005

    def _apply_profile(self, profile):
        self.use_loopback = profile["use_loopback"]
        self.base_sensitivity = profile["sensitivity"]
        self.silence_sec = profile["silence_sec"]
        self.gain = profile["gain"]

    def set_mode(self, mode_key: str):
        if mode_key in MODE_PROFILES:
            self.current_mode = mode_key
            self._apply_profile(MODE_PROFILES[mode_key])
            self.mode_changed = True

    def toggle_pause(self) -> bool:
        self.is_paused = not self.is_paused
        return self.is_paused

    def run(self):
        self.running = True
        sample_rate = DEFAULT_SAMPLE_RATE
        chunk_frames = int(sample_rate * CHUNK_DURATION_SEC)
        min_chunks_needed = int(MIN_SPEECH_DURATION_SEC / CHUNK_DURATION_SEC)
        max_chunks_limit = int(MAX_SPEECH_DURATION_SEC / CHUNK_DURATION_SEC)

        while self.running:
            self.mode_changed = False
            silence_chunks_needed = int(self.silence_sec / CHUNK_DURATION_SEC)

            try:
                # Dynamic re-query handles Bluetooth / Headphone plug/unplug events
                if self.use_loopback:
                    spk = sc.default_speaker()
                    dev_name = spk.name if spk else "Default Speaker"
                    mic_device = sc.get_microphone(id=str(dev_name), include_loopback=True)
                    self.status_callback(f"Listening: Online Call ({dev_name[:18]}...)")
                else:
                    mic_device = sc.default_microphone()
                    dev_name = mic_device.name if mic_device else "Microphone"
                    self.status_callback(f"Listening: In-Person Room Mic ({dev_name[:16]}...)")

                with mic_device.recorder(samplerate=sample_rate, channels=1) as rec:
                    speech_frames = []
                    silence_counter = 0
                    is_speaking = False
                    pre_buffer = []
                    recent_energies = []

                    while self.running and not self.mode_changed:
                        if self.is_paused:
                            time.sleep(0.1)
                            continue

                        chunk = rec.record(numframes=chunk_frames)
                        mono = chunk[:, 0] if chunk.ndim > 1 else chunk

                        # Far-field room gain boost
                        if self.gain != 1.0:
                            mono = np.clip(mono * self.gain, -1.0, 1.0)

                        rms = float(np.sqrt(np.mean(mono**2)))
                        self.level_callback(min(1.0, rms * 35))

                        # Adaptive Noise Floor Tracking
                        recent_energies.append(rms)
                        if len(recent_energies) > 30:  # ~1.8 seconds window
                            recent_energies.pop(0)
                            self.noise_floor = min(recent_energies) * 0.95

                        effective_threshold = max(self.base_sensitivity, self.noise_floor * 1.8)

                        if rms > effective_threshold:
                            if not is_speaking:
                                is_speaking = True
                                speech_frames.extend(pre_buffer)
                                pre_buffer.clear()
                            speech_frames.append(mono)
                            silence_counter = 0
                        else:
                            if is_speaking:
                                speech_frames.append(mono)
                                silence_counter += 1
                                if silence_counter >= silence_chunks_needed or len(speech_frames) >= max_chunks_limit:
                                    if len(speech_frames) >= min_chunks_needed:
                                        full_audio = np.concatenate(speech_frames)
                                        pcm_bytes = (full_audio * 32767).clip(-32768, 32767).astype(np.int16).tobytes()
                                        self.audio_queue.put(pcm_bytes)
                                    speech_frames.clear()
                                    is_speaking = False
                                    silence_counter = 0
                            else:
                                pre_buffer.append(mono)
                                if len(pre_buffer) > 4:  # retain ~240ms pre-voice buffer
                                    pre_buffer.pop(0)

            except Exception:
                self.status_callback("Audio reconnecting in 1s...")
                time.sleep(1.0)


class TranslationWorker(threading.Thread):
    """Processes speech buffers through Speech-to-Text and contextual Telugu translation."""

    def __init__(self, audio_queue, result_callback, status_callback, get_target_lang, get_accent):
        super().__init__(daemon=True)
        self.audio_queue = audio_queue
        self.result_callback = result_callback
        self.status_callback = status_callback
        self.get_target_lang = get_target_lang
        self.get_accent = get_accent
        self.recognizer = sr.Recognizer()
        self.recognizer.dynamic_energy_threshold = False
        self.running = True

    def run(self):
        while self.running:
            try:
                pcm_bytes = self.audio_queue.get(timeout=0.8)
            except queue.Empty:
                continue

            try:
                self.status_callback("⚡ Transcribing client speech...")
                audio_data = sr.AudioData(pcm_bytes, sample_rate=DEFAULT_SAMPLE_RATE, sample_width=2)
                accent = self.get_accent()
                # Fast Google Speech Recognition with client accent model
                english_text = self.recognizer.recognize_google(audio_data, language=accent)

                if english_text and len(english_text.strip()) > 1:
                    target_lang = self.get_target_lang()
                    lang_name = [k for k, v in SUPPORTED_LANGUAGES.items() if v == target_lang]
                    lang_display = lang_name[0].split()[0] if lang_name else "Telugu"
                    self.status_callback(f"Translating to {lang_display}...")

                    telugu_text = translate_text(english_text, target_lang=target_lang)
                    self.result_callback(english_text, telugu_text)
                    self.status_callback("Ready (Listening...)")
            except sr.UnknownValueError:
                self.status_callback("Ready (Listening...)")
            except sr.RequestError:
                self.status_callback("Network connection notice")
                time.sleep(1)
            except Exception as e:
                self.status_callback(f"Notice: {str(e)[:30]}")


class CallTranslatorHUD(tk.Tk):
    """Enterprise HUD overlay with instant mode switching, font scaling, accent picker, and pause toggle."""

    def __init__(self):
        super().__init__()

        self.title("Instant Call Translator")
        self.geometry("920x225+220+35")
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.94)
        self.overrideredirect(True)
        self.configure(bg="#121316")

        self.target_lang_code = "te"  # Default Telugu
        self.accent_code = "en-US"  # Default US accent
        self.current_mode = "online"
        self.telugu_font_size = 16  # Scalable font
        self.history = []
        self.drag_x = 0
        self.drag_y = 0

        self.audio_queue = queue.Queue()

        self._build_ui()

        # Start Background Workers
        self.capture_worker = AudioCaptureWorker(
            self.audio_queue,
            self.update_status_threadsafe,
            self.update_level_threadsafe,
        )
        self.translation_worker = TranslationWorker(
            self.audio_queue,
            self.update_subtitles_threadsafe,
            self.update_status_threadsafe,
            lambda: self.target_lang_code,
            lambda: self.accent_code,
        )

        self.capture_worker.start()
        self.translation_worker.start()

    def _build_ui(self):
        # Top Draggable Header Bar
        self.header_bar = tk.Frame(self, bg="#1a1c22", height=34, cursor="fleur")
        self.header_bar.pack(fill=tk.X, side=tk.TOP)
        self.header_bar.bind("<Button-1>", self._start_drag)
        self.header_bar.bind("<B1-Motion>", self._on_drag)

        # Brand
        title_lbl = tk.Label(
            self.header_bar,
            text="⚡ Instant Translator",
            font=("Segoe UI", 9, "bold"),
            fg="#00e5ff",
            bg="#1a1c22",
        )
        title_lbl.pack(side=tk.LEFT, padx=(10, 4))
        title_lbl.bind("<Button-1>", self._start_drag)
        title_lbl.bind("<B1-Motion>", self._on_drag)

        # Audio Mode Buttons
        self.btn_online = tk.Button(
            self.header_bar,
            text="🎧 Online Call",
            font=("Segoe UI", 8, "bold"),
            bg="#006064",
            fg="#e0f7fa",
            activebackground="#00838f",
            activeforeground="#ffffff",
            bd=0,
            padx=7,
            pady=2,
            relief=tk.FLAT,
            command=lambda: self._select_mode("online"),
        )
        self.btn_online.pack(side=tk.LEFT, padx=2)

        self.btn_offline = tk.Button(
            self.header_bar,
            text="🎙️ Room Mic",
            font=("Segoe UI", 8),
            bg="#263238",
            fg="#b0bec5",
            activebackground="#37474f",
            activeforeground="#ffffff",
            bd=0,
            padx=7,
            pady=2,
            relief=tk.FLAT,
            command=lambda: self._select_mode("offline_room"),
        )
        self.btn_offline.pack(side=tk.LEFT, padx=2)

        # Accent Selector Dropdown
        self.accent_var = tk.StringVar(value="🇺🇸 US Client (en-US)")
        accent_combo = ttk.Combobox(
            self.header_bar,
            textvariable=self.accent_var,
            values=list(CLIENT_ACCENTS.keys()),
            state="readonly",
            width=17,
            font=("Segoe UI", 8),
        )
        accent_combo.pack(side=tk.LEFT, padx=4)
        accent_combo.bind("<<ComboboxSelected>>", self._on_accent_changed)

        # Target Language Dropdown
        self.lang_var = tk.StringVar(value="Telugu (తెలుగు)")
        lang_combo = ttk.Combobox(
            self.header_bar,
            textvariable=self.lang_var,
            values=list(SUPPORTED_LANGUAGES.keys()),
            state="readonly",
            width=15,
            font=("Segoe UI", 8),
        )
        lang_combo.pack(side=tk.LEFT, padx=4)
        lang_combo.bind("<<ComboboxSelected>>", self._on_lang_changed)

        # Font Resizing Controls
        self.font_down_btn = tk.Button(
            self.header_bar,
            text=" A- ",
            font=("Segoe UI", 8, "bold"),
            bg="#263238",
            fg="#cfd8dc",
            bd=0,
            padx=3,
            pady=2,
            relief=tk.FLAT,
            command=self._decrease_font,
        )
        self.font_down_btn.pack(side=tk.LEFT, padx=1)

        self.font_up_btn = tk.Button(
            self.header_bar,
            text=" A+ ",
            font=("Segoe UI", 8, "bold"),
            bg="#263238",
            fg="#cfd8dc",
            bd=0,
            padx=3,
            pady=2,
            relief=tk.FLAT,
            command=self._increase_font,
        )
        self.font_up_btn.pack(side=tk.LEFT, padx=1)

        # Pause / Resume Button
        self.pause_btn = tk.Button(
            self.header_bar,
            text="⏸️ Pause",
            font=("Segoe UI", 8, "bold"),
            bg="#37474f",
            fg="#eceff1",
            activebackground="#455a64",
            activeforeground="#ffffff",
            bd=0,
            padx=6,
            pady=2,
            relief=tk.FLAT,
            command=self._toggle_pause,
        )
        self.pause_btn.pack(side=tk.LEFT, padx=4)

        # History Button
        hist_btn = tk.Button(
            self.header_bar,
            text="📜 History",
            font=("Segoe UI", 8),
            bg="#2d3139",
            fg="#cfd8dc",
            activebackground="#3e4451",
            activeforeground="#ffffff",
            bd=0,
            padx=6,
            pady=2,
            relief=tk.FLAT,
            command=self._show_history_window,
        )
        hist_btn.pack(side=tk.LEFT, padx=2)

        # Close Button
        close_btn = tk.Button(
            self.header_bar,
            text=" ✕ ",
            font=("Segoe UI", 9, "bold"),
            bg="#b71c1c",
            fg="#ffffff",
            activebackground="#d32f2f",
            activeforeground="#ffffff",
            bd=0,
            relief=tk.FLAT,
            command=self._exit_app,
        )
        close_btn.pack(side=tk.RIGHT, padx=4, pady=3)

        # Compact Button
        self.compact_mode = False
        self.min_btn = tk.Button(
            self.header_bar,
            text=" ─ ",
            font=("Segoe UI", 9, "bold"),
            bg="#37474f",
            fg="#ffffff",
            activebackground="#455a64",
            activeforeground="#ffffff",
            bd=0,
            relief=tk.FLAT,
            command=self._toggle_compact,
        )
        self.min_btn.pack(side=tk.RIGHT, padx=2, pady=3)

        # Subtitles Display Container
        self.content_frame = tk.Frame(self, bg="#121316", padx=16, pady=8)
        self.content_frame.pack(fill=tk.BOTH, expand=True)

        # Spoken English
        self.english_lbl = tk.Label(
            self.content_frame,
            text="[English speech from call or room will display here...]",
            font=("Segoe UI", 11, "italic"),
            fg="#90a4ae",
            bg="#121316",
            wraplength=880,
            justify=tk.LEFT,
            anchor="w",
        )
        self.english_lbl.pack(fill=tk.X, pady=(2, 4))

        # Translated Telugu
        self.telugu_lbl = tk.Label(
            self.content_frame,
            text="[ఇక్కడ మాట్లాడే మాటలు వెంటనే తెలుగులో అనువదించబడతాయి]",
            font=("Nirmala UI", self.telugu_font_size, "bold"),
            fg="#ffe082",
            bg="#121316",
            wraplength=880,
            justify=tk.LEFT,
            anchor="w",
        )
        self.telugu_lbl.pack(fill=tk.X, pady=(2, 6))

        # Bottom Footer Bar
        self.footer_bar = tk.Frame(self, bg="#16181d", height=24)
        self.footer_bar.pack(fill=tk.X, side=tk.BOTTOM)

        self.status_lbl = tk.Label(
            self.footer_bar,
            text="🟢 Audio listener active...",
            font=("Segoe UI", 8),
            fg="#81c784",
            bg="#16181d",
        )
        self.status_lbl.pack(side=tk.LEFT, padx=10)

        # Active Mode Hint
        self.mode_desc_lbl = tk.Label(
            self.footer_bar,
            text="Mode: 🎧 Online Call (PC Speakers)",
            font=("Segoe UI", 8, "italic"),
            fg="#80deea",
            bg="#16181d",
        )
        self.mode_desc_lbl.pack(side=tk.LEFT, padx=8)

        # Audio activity indicator
        self.level_canvas = tk.Canvas(self.footer_bar, width=80, height=8, bg="#263238", highlightthickness=0)
        self.level_canvas.pack(side=tk.RIGHT, padx=10, pady=6)
        self.level_bar = self.level_canvas.create_rectangle(0, 0, 0, 8, fill="#00e676", outline="")

    def _start_drag(self, event):
        self.drag_x = event.x
        self.drag_y = event.y

    def _on_drag(self, event):
        x = self.winfo_x() + (event.x - self.drag_x)
        y = self.winfo_y() + (event.y - self.drag_y)
        self.geometry(f"+{x}+{y}")

    def _select_mode(self, mode_key: str):
        self.current_mode = mode_key
        self.capture_worker.set_mode(mode_key)

        if mode_key == "online":
            self.btn_online.config(bg="#006064", fg="#e0f7fa", font=("Segoe UI", 8, "bold"))
            self.btn_offline.config(bg="#263238", fg="#b0bec5", font=("Segoe UI", 8))
            self.mode_desc_lbl.config(text="Mode: 🎧 Online Call (PC Speakers)", fg="#80deea")
        else:
            self.btn_offline.config(bg="#e65100", fg="#fff3e0", font=("Segoe UI", 8, "bold"))
            self.btn_online.config(bg="#263238", fg="#b0bec5", font=("Segoe UI", 8))
            self.mode_desc_lbl.config(text="Mode: 🎙️ In-Person Meeting (Room Mic + 2.2x Gain)", fg="#ffcc80")

    def _on_accent_changed(self, event=None):
        name = self.accent_var.get()
        self.accent_code = CLIENT_ACCENTS.get(name, "en-US")
        self.status_lbl.config(text=f"Spoken accent: {self.accent_code}")

    def _on_lang_changed(self, event=None):
        name = self.lang_var.get()
        self.target_lang_code = SUPPORTED_LANGUAGES.get(name, "te")
        self.status_lbl.config(text=f"Target language: {name.split()[0]}")

    def _increase_font(self):
        if self.telugu_font_size < 24:
            self.telugu_font_size += 2
            self.telugu_lbl.config(font=("Nirmala UI", self.telugu_font_size, "bold"))

    def _decrease_font(self):
        if self.telugu_font_size > 12:
            self.telugu_font_size -= 2
            self.telugu_lbl.config(font=("Nirmala UI", self.telugu_font_size, "bold"))

    def _toggle_pause(self):
        is_paused = self.capture_worker.toggle_pause()
        if is_paused:
            self.pause_btn.config(text="▶️ Resume", bg="#c62828", fg="#ffffff")
            self.status_lbl.config(text="⏸️ Translation Paused (Audio muted)")
        else:
            self.pause_btn.config(text="⏸️ Pause", bg="#37474f", fg="#eceff1")
            self.status_lbl.config(text="🟢 Listening resumed...")

    def _toggle_compact(self):
        self.compact_mode = not self.compact_mode
        if self.compact_mode:
            self.geometry(f"{self.winfo_width()}x100")
            self.english_lbl.pack_forget()
            self.min_btn.config(text=" ▢ ")
        else:
            self.geometry(f"{self.winfo_width()}x225")
            self.english_lbl.pack(fill=tk.X, pady=(2, 4), before=self.telugu_lbl)
            self.min_btn.config(text=" ─ ")

    def update_subtitles_threadsafe(self, english: str, translated: str):
        self.after(0, self._set_subtitles, english, translated)

    def _set_subtitles(self, english: str, translated: str):
        self.english_lbl.config(text=f"🇬🇧 {english}")
        self.telugu_lbl.config(text=f"🇮🇳 {translated}")
        timestamp = time.strftime("%H:%M:%S")
        self.history.append((timestamp, english, translated))

    def update_status_threadsafe(self, text: str):
        self.after(0, lambda: self.status_lbl.config(text=text))

    def update_level_threadsafe(self, level: float):
        bar_width = int(level * 80)
        color = "#ff9100" if self.current_mode == "offline_room" else "#00e676"
        self.after(0, lambda: self._update_bar(bar_width, color))

    def _update_bar(self, width: int, color: str):
        self.level_canvas.itemconfig(self.level_bar, fill=color)
        self.level_canvas.coords(self.level_bar, 0, 0, width, 8)

    def _show_history_window(self):
        hw = tk.Toplevel(self)
        hw.title("Meeting Transcript History")
        hw.geometry("720x460")
        hw.attributes("-topmost", True)
        hw.configure(bg="#1e2025")

        txt_frame = tk.Frame(hw, bg="#1e2025")
        txt_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        scrollbar = tk.Scrollbar(txt_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        text_area = tk.Text(
            txt_frame,
            wrap=tk.WORD,
            yscrollcommand=scrollbar.set,
            bg="#121316",
            fg="#e0e0e0",
            font=("Nirmala UI", 11),
            padx=10,
            pady=10,
        )
        text_area.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=text_area.yview)

        if not self.history:
            text_area.insert(tk.END, "No meeting speech recorded yet.")
        else:
            for ts, en, tr in self.history:
                text_area.insert(tk.END, f"[{ts}] EN: {en}\n", "en_tag")
                text_area.insert(tk.END, f"[{ts}] TR: {tr}\n\n", "tr_tag")

        text_area.tag_config("en_tag", foreground="#90a4ae")
        text_area.tag_config("tr_tag", foreground="#ffe082", font=("Nirmala UI", 12, "bold"))
        text_area.config(state=tk.DISABLED)

        btn_bar = tk.Frame(hw, bg="#1e2025")
        btn_bar.pack(fill=tk.X, padx=10, pady=(0, 10))

        def save_log():
            log_path = os.path.join(os.path.expanduser("~"), "Desktop", f"meeting_transcript_{int(time.time())}.txt")
            with open(log_path, "w", encoding="utf-8") as f:
                for ts, en, tr in self.history:
                    f.write(f"[{ts}] English: {en}\n[{ts}] Telugu:  {tr}\n\n")
            messagebox.showinfo("Saved", f"Transcript saved to Desktop:\n{log_path}")

        save_btn = tk.Button(btn_bar, text="💾 Save to Desktop", command=save_log, bg="#2e7d32", fg="#ffffff", relief=tk.FLAT, padx=10)
        save_btn.pack(side=tk.LEFT)

    def _exit_app(self):
        self.capture_worker.running = False
        self.translation_worker.running = False
        self.destroy()
        sys.exit(0)


if __name__ == "__main__":
    app = CallTranslatorHUD()
    app.mainloop()
