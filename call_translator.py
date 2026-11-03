"""
Instant Call Translator (English -> Telugu & other Indian languages)
Floating HUD Overlay for Microsoft Teams, Zoom, Google Meet & Offline In-Person Meetings.
"""

import os
import sys
import time
import math
import queue
import logging
import warnings
import threading
import tkinter as tk
from tkinter import ttk, messagebox
import requests
import numpy as np

# Audio & Speech Recognition
import soundcard as sc
import speech_recognition as sr

# Suppress soundcard buffer warnings
warnings.filterwarnings("ignore")

# Configuration Constants
DEFAULT_SAMPLE_RATE = 16000
CHUNK_DURATION_SEC = 0.06  # 60ms chunks for rapid VAD response
MIN_SPEECH_DURATION_SEC = 0.35  # Ignore transient noise under 350ms
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

# Mode Profiles
MODE_PROFILES = {
    "online": {
        "name": "🎧 Online Call (Teams/Zoom)",
        "use_loopback": True,
        "sensitivity": 0.015,
        "silence_sec": 0.55,
        "gain": 1.0,
        "badge": "#00bcd4",
        "desc": "Captures client voice from speakers/headphones",
    },
    "offline_room": {
        "name": "🎙️ Offline Meeting (Room Mic)",
        "use_loopback": False,
        "sensitivity": 0.007,  # Higher sensitivity for distant voices
        "silence_sec": 0.45,  # Snappier finalization for instant translation
        "gain": 2.2,  # Software gain to amplify speakers across the table
        "badge": "#ff9800",
        "desc": "High-gain room mic for in-person conference meetings",
    },
    "mic_close": {
        "name": "🎤 Direct Mic (Headset / Solo)",
        "use_loopback": False,
        "sensitivity": 0.015,
        "silence_sec": 0.50,
        "gain": 1.0,
        "badge": "#4caf50",
        "desc": "Standard mic for direct close-up speech",
    },
}


def translate_text(text: str, target_lang: str = "te") -> str:
    """Translates text from English to target language with instant fallback."""
    if not text or not text.strip():
        return ""
    if target_lang == "en":
        return text

    # Attempt 1: Fast Google GTX API (~150-300ms)
    try:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {
            "client": "gtx",
            "sl": "en",
            "tl": target_lang,
            "dt": "t",
            "q": text.strip(),
        }
        res = requests.get(url, params=params, timeout=3.5)
        if res.status_code == 200:
            data = res.json()
            translated = "".join(part[0] for part in data[0] if part[0])
            if translated:
                return translated
    except Exception:
        pass

    # Attempt 2: MyMemory fallback via deep-translator
    try:
        from deep_translator import MyMemoryTranslator

        lang_code_map = {"te": "te-IN", "hi": "hi-IN", "ta": "ta-IN", "kn": "kn-IN"}
        dest = lang_code_map.get(target_lang, target_lang)
        fallback_res = MyMemoryTranslator(source="en-GB", target=dest).translate(text)
        if fallback_res:
            return fallback_res
    except Exception:
        pass

    return text


class AudioCaptureWorker(threading.Thread):
    """Captures audio, handles far-field gain & VAD, and dispatches speech utterances."""

    def __init__(self, audio_queue, status_callback, level_callback):
        super().__init__(daemon=True)
        self.audio_queue = audio_queue
        self.status_callback = status_callback
        self.level_callback = level_callback
        self.running = False

        # Current profile settings
        self.current_mode = "online"
        self._apply_profile(MODE_PROFILES[self.current_mode])
        self.mode_changed = False

    def _apply_profile(self, profile):
        self.use_loopback = profile["use_loopback"]
        self.sensitivity = profile["sensitivity"]
        self.silence_sec = profile["silence_sec"]
        self.gain = profile["gain"]

    def set_mode(self, mode_key: str):
        if mode_key in MODE_PROFILES:
            self.current_mode = mode_key
            self._apply_profile(MODE_PROFILES[mode_key])
            self.mode_changed = True

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
                if self.use_loopback:
                    spk = sc.default_speaker()
                    dev_name = spk.name if spk else "Default Speaker"
                    mic_device = sc.get_microphone(id=str(dev_name), include_loopback=True)
                    self.status_callback(f"Listening: Online Call ({dev_name[:20]}...)")
                else:
                    mic_device = sc.default_microphone()
                    dev_name = mic_device.name if mic_device else "Microphone"
                    mode_label = "In-Person Room" if self.current_mode == "offline_room" else "Direct Mic"
                    self.status_callback(f"Listening: {mode_label} ({dev_name[:18]}...)")

                with mic_device.recorder(samplerate=sample_rate, channels=1) as rec:
                    speech_frames = []
                    silence_counter = 0
                    is_speaking = False
                    pre_buffer = []

                    while self.running and not self.mode_changed:
                        chunk = rec.record(numframes=chunk_frames)
                        mono = chunk[:, 0] if chunk.ndim > 1 else chunk

                        # Apply software gain boost (crucial for room meetings)
                        if self.gain != 1.0:
                            mono = np.clip(mono * self.gain, -1.0, 1.0)

                        rms = float(np.sqrt(np.mean(mono**2)))
                        self.level_callback(min(1.0, rms * 35))

                        if rms > self.sensitivity:
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
                                if len(pre_buffer) > 4:  # retain ~240ms before voice onset
                                    pre_buffer.pop(0)

            except Exception as e:
                self.status_callback("Audio stream resetting in 1.5s...")
                time.sleep(1.5)


class TranslationWorker(threading.Thread):
    """Processes audio queue -> Fast STT -> Machine Translation -> GUI update."""

    def __init__(self, audio_queue, result_callback, status_callback, get_target_lang):
        super().__init__(daemon=True)
        self.audio_queue = audio_queue
        self.result_callback = result_callback
        self.status_callback = status_callback
        self.get_target_lang = get_target_lang
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
                self.status_callback("Transcribing voice...")
                audio_data = sr.AudioData(pcm_bytes, sample_rate=DEFAULT_SAMPLE_RATE, sample_width=2)
                # Google Web Speech Recognition
                english_text = self.recognizer.recognize_google(audio_data, language="en-US")

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
                self.status_callback("Network connection issue")
                time.sleep(1)
            except Exception as e:
                self.status_callback(f"Notice: {str(e)[:30]}")


class CallTranslatorHUD(tk.Tk):
    """Sleek, transparent, always-on-top HUD floating subtitle overlay with multi-mode audio."""

    def __init__(self):
        super().__init__()

        self.title("Instant Call Translator")
        self.geometry("860x220+240+35")
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.94)
        self.overrideredirect(True)
        self.configure(bg="#121316")

        self.target_lang_code = "te"  # Default Telugu
        self.current_mode = "online"
        self.history = []
        self.drag_x = 0
        self.drag_y = 0

        self.audio_queue = queue.Queue()

        self._build_ui()

        # Start Workers
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
        )

        self.capture_worker.start()
        self.translation_worker.start()

    def _build_ui(self):
        # Top draggable header bar
        self.header_bar = tk.Frame(self, bg="#1a1c22", height=34, cursor="fleur")
        self.header_bar.pack(fill=tk.X, side=tk.TOP)
        self.header_bar.bind("<Button-1>", self._start_drag)
        self.header_bar.bind("<B1-Motion>", self._on_drag)

        # App Brand
        title_lbl = tk.Label(
            self.header_bar,
            text="⚡ Instant Translator",
            font=("Segoe UI", 9, "bold"),
            fg="#00e5ff",
            bg="#1a1c22",
        )
        title_lbl.pack(side=tk.LEFT, padx=(10, 6))
        title_lbl.bind("<Button-1>", self._start_drag)
        title_lbl.bind("<B1-Motion>", self._on_drag)

        # Mode Selector Buttons
        self.mode_frame = tk.Frame(self.header_bar, bg="#1a1c22")
        self.mode_frame.pack(side=tk.LEFT, padx=4)

        self.btn_online = tk.Button(
            self.mode_frame,
            text="🎧 Online Call (Teams/Zoom)",
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
            self.mode_frame,
            text="🎙️ Offline Meeting (Room Mic)",
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
        lang_combo.pack(side=tk.LEFT, padx=6)
        lang_combo.bind("<<ComboboxSelected>>", self._on_lang_changed)

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
        hist_btn.pack(side=tk.LEFT, padx=3)

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
            wraplength=820,
            justify=tk.LEFT,
            anchor="w",
        )
        self.english_lbl.pack(fill=tk.X, pady=(2, 4))

        # Translated Telugu
        self.telugu_lbl = tk.Label(
            self.content_frame,
            text="[ఇక్కడ మాట్లాడే మాటలు వెంటనే తెలుగులో అనువదించబడతాయి]",
            font=("Nirmala UI", 16, "bold"),
            fg="#ffe082",
            bg="#121316",
            wraplength=820,
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
            text="Mode: Online Call (Speakers)",
            font=("Segoe UI", 8, "italic"),
            fg="#90a4ae",
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
        profile = MODE_PROFILES[mode_key]

        if mode_key == "online":
            self.btn_online.config(bg="#006064", fg="#e0f7fa", font=("Segoe UI", 8, "bold"))
            self.btn_offline.config(bg="#263238", fg="#b0bec5", font=("Segoe UI", 8))
            self.mode_desc_lbl.config(text="Mode: 🎧 Online Call (PC Speakers)", fg="#80deea")
        else:
            self.btn_offline.config(bg="#e65100", fg="#fff3e0", font=("Segoe UI", 8, "bold"))
            self.btn_online.config(bg="#263238", fg="#b0bec5", font=("Segoe UI", 8))
            self.mode_desc_lbl.config(text="Mode: 🎙️ In-Person Meeting (Room Mic + Gain)", fg="#ffcc80")

    def _on_lang_changed(self, event=None):
        name = self.lang_var.get()
        self.target_lang_code = SUPPORTED_LANGUAGES.get(name, "te")
        self.status_lbl.config(text=f"Target language: {name.split()[0]}")

    def _toggle_compact(self):
        self.compact_mode = not self.compact_mode
        if self.compact_mode:
            self.geometry(f"{self.winfo_width()}x100")
            self.english_lbl.pack_forget()
            self.min_btn.config(text=" ▢ ")
        else:
            self.geometry(f"{self.winfo_width()}x220")
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
