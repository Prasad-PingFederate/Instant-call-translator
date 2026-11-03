"""
Real-Time Call Audio Translator (English -> Telugu & other Indian languages)
Floating HUD Overlay for Microsoft Teams, Zoom, Google Meet & Web Calls.
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
CHUNK_DURATION_SEC = 0.08  # 80ms chunks for responsive VAD
SILENCE_DURATION_SEC = 0.65  # 650ms silence marks end of sentence
MIN_SPEECH_DURATION_SEC = 0.4  # ignore pops/clicks under 400ms
MAX_SPEECH_DURATION_SEC = 9.0  # max sentence chunk to translate

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


def translate_text(text: str, target_lang: str = "te") -> str:
    """Translates text from English to target language with instant fallback."""
    if not text or not text.strip():
        return ""
    if target_lang == "en":
        return text

    # Attempt 1: Fast Google GTX API
    try:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {
            "client": "gtx",
            "sl": "en",
            "tl": target_lang,
            "dt": "t",
            "q": text.strip(),
        }
        res = requests.get(url, params=params, timeout=4)
        if res.status_code == 200:
            data = res.json()
            translated = "".join(part[0] for part in data[0] if part[0])
            if translated:
                return translated
    except Exception as e:
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
    """Background thread that captures audio, tracks speech activity, and feeds audio chunks."""

    def __init__(self, audio_queue, status_callback, level_callback):
        super().__init__(daemon=True)
        self.audio_queue = audio_queue
        self.status_callback = status_callback
        self.level_callback = level_callback
        self.running = False
        self.use_loopback = True  # True: Speaker Loopback (Teams/Zoom call), False: Mic
        self.sensitivity = 0.015  # RMS energy threshold for speech

    def run(self):
        self.running = True
        sample_rate = DEFAULT_SAMPLE_RATE
        chunk_frames = int(sample_rate * CHUNK_DURATION_SEC)
        silence_chunks_needed = int(SILENCE_DURATION_SEC / CHUNK_DURATION_SEC)
        min_chunks_needed = int(MIN_SPEECH_DURATION_SEC / CHUNK_DURATION_SEC)
        max_chunks_limit = int(MAX_SPEECH_DURATION_SEC / CHUNK_DURATION_SEC)

        while self.running:
            try:
                if self.use_loopback:
                    spk = sc.default_speaker()
                    device_name = spk.name if spk else "Default Speaker"
                    mic_device = sc.get_microphone(id=str(device_name), include_loopback=True)
                    self.status_callback(f"Listening: Call Audio ({device_name[:24]}...)")
                else:
                    mic_device = sc.default_microphone()
                    dev_name = mic_device.name if mic_device else "Microphone"
                    self.status_callback(f"Listening: Mic ({dev_name[:24]}...)")

                with mic_device.recorder(samplerate=sample_rate, channels=1) as rec:
                    speech_frames = []
                    silence_counter = 0
                    is_speaking = False
                    pre_buffer = []

                    while self.running:
                        chunk = rec.record(numframes=chunk_frames)
                        # Mono float32 array (-1.0 to 1.0)
                        mono = chunk[:, 0] if chunk.ndim > 1 else chunk
                        rms = float(np.sqrt(np.mean(mono**2)))
                        self.level_callback(min(1.0, rms * 30))

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
                                        # Convert float32 to int16 PCM bytes
                                        pcm_bytes = (full_audio * 32767).clip(-32768, 32767).astype(np.int16).tobytes()
                                        self.audio_queue.put(pcm_bytes)
                                    speech_frames.clear()
                                    is_speaking = False
                                    silence_counter = 0
                            else:
                                pre_buffer.append(mono)
                                if len(pre_buffer) > 4:  # retain ~320ms before voice onset
                                    pre_buffer.pop(0)

            except Exception as e:
                self.status_callback(f"Audio device error. Retrying in 2s...")
                time.sleep(2)


class TranslationWorker(threading.Thread):
    """Processes audio queue -> Speech-to-Text -> Machine Translation -> GUI update."""

    def __init__(self, audio_queue, result_callback, status_callback, get_target_lang):
        super().__init__(daemon=True)
        self.audio_queue = audio_queue
        self.result_callback = result_callback
        self.status_callback = status_callback
        self.get_target_lang = get_target_lang
        self.recognizer = sr.Recognizer()
        self.running = True

    def run(self):
        while self.running:
            try:
                pcm_bytes = self.audio_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            try:
                self.status_callback("Transcribing English...")
                audio_data = sr.AudioData(pcm_bytes, sample_rate=DEFAULT_SAMPLE_RATE, sample_width=2)
                # Free Google Speech Recognition API
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
            except sr.RequestError as e:
                self.status_callback("Network error recognizing speech")
                time.sleep(1)
            except Exception as e:
                self.status_callback(f"Translation notice: {str(e)[:30]}")


class CallTranslatorHUD(tk.Tk):
    """Sleek, transparent, always-on-top HUD floating subtitle overlay."""

    def __init__(self):
        super().__init__()

        self.title("Instant Call Translator")
        self.geometry("820x210+260+40")  # Positioned top-center
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.93)
        self.overrideredirect(True)  # Frameless modern HUD
        self.configure(bg="#121316")

        self.target_lang_code = "te"  # Default Telugu
        self.history = []  # Stores (timestamp, english, telugu)
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
        )

        self.capture_worker.start()
        self.translation_worker.start()

    def _build_ui(self):
        # Top draggable title / header bar
        self.header_bar = tk.Frame(self, bg="#1e2025", height=32, cursor="fleur")
        self.header_bar.pack(fill=tk.X, side=tk.TOP)
        self.header_bar.bind("<Button-1>", self._start_drag)
        self.header_bar.bind("<B1-Motion>", self._on_drag)

        title_lbl = tk.Label(
            self.header_bar,
            text="🌐 Call Auto-Translator",
            font=("Segoe UI", 9, "bold"),
            fg="#64b5f6",
            bg="#1e2025",
        )
        title_lbl.pack(side=tk.LEFT, padx=10)
        title_lbl.bind("<Button-1>", self._start_drag)
        title_lbl.bind("<B1-Motion>", self._on_drag)

        # Audio Source Selector Toggle
        self.source_var = tk.StringVar(value="loopback")
        self.source_btn = tk.Button(
            self.header_bar,
            text="🎧 Audio: Call Output (Speakers)",
            font=("Segoe UI", 8, "bold"),
            bg="#263238",
            fg="#80cbc4",
            activebackground="#37474f",
            activeforeground="#ffffff",
            bd=0,
            padx=8,
            pady=2,
            relief=tk.FLAT,
            command=self._toggle_source,
        )
        self.source_btn.pack(side=tk.LEFT, padx=6)

        # Language dropdown
        self.lang_var = tk.StringVar(value="Telugu (తెలుగు)")
        lang_combo = ttk.Combobox(
            self.header_bar,
            textvariable=self.lang_var,
            values=list(SUPPORTED_LANGUAGES.keys()),
            state="readonly",
            width=16,
            font=("Segoe UI", 8),
        )
        lang_combo.pack(side=tk.LEFT, padx=4)
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
        hist_btn.pack(side=tk.LEFT, padx=4)

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

        # Minimize / Compact Button
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

        # Subtitle Content Container
        self.content_frame = tk.Frame(self, bg="#121316", padx=16, pady=8)
        self.content_frame.pack(fill=tk.BOTH, expand=True)

        # Spoken English text (smaller, gray, italic)
        self.english_lbl = tk.Label(
            self.content_frame,
            text="[English client speech will show here...]",
            font=("Segoe UI", 11, "italic"),
            fg="#90a4ae",
            bg="#121316",
            wraplength=780,
            justify=tk.LEFT,
            anchor="w",
        )
        self.english_lbl.pack(fill=tk.X, pady=(2, 4))

        # Translated Telugu text (large, bold, high-contrast gold/cyan)
        self.telugu_lbl = tk.Label(
            self.content_frame,
            text="[క్లయింట్ మాట్లాడే మాటలు ఇక్కడ తెలుగులో అనువదించబడతాయి]",
            font=("Nirmala UI", 16, "bold"),
            fg="#ffe082",
            bg="#121316",
            wraplength=780,
            justify=tk.LEFT,
            anchor="w",
        )
        self.telugu_lbl.pack(fill=tk.X, pady=(2, 6))

        # Bottom Status / Activity Bar
        self.footer_bar = tk.Frame(self, bg="#181a1f", height=24)
        self.footer_bar.pack(fill=tk.X, side=tk.BOTTOM)

        self.status_lbl = tk.Label(
            self.footer_bar,
            text="🟢 Initializing audio listener...",
            font=("Segoe UI", 8),
            fg="#81c784",
            bg="#181a1f",
        )
        self.status_lbl.pack(side=tk.LEFT, padx=10)

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

    def _toggle_source(self):
        if self.source_var.get() == "loopback":
            self.source_var.set("mic")
            self.capture_worker.use_loopback = False
            self.source_btn.config(text="🎙️ Audio: My Microphone (Testing)", bg="#bf360c", fg="#ffccbc")
        else:
            self.source_var.set("loopback")
            self.capture_worker.use_loopback = True
            self.source_btn.config(text="🎧 Audio: Call Output (Speakers)", bg="#263238", fg="#80cbc4")

    def _on_lang_changed(self, event=None):
        name = self.lang_var.get()
        self.target_lang_code = SUPPORTED_LANGUAGES.get(name, "te")
        self.status_lbl.config(text=f"Switched target language to {name.split()[0]}")

    def _toggle_compact(self):
        self.compact_mode = not self.compact_mode
        if self.compact_mode:
            self.geometry(f"{self.winfo_width()}x100")
            self.english_lbl.pack_forget()
            self.min_btn.config(text=" ▢ ")
        else:
            self.geometry(f"{self.winfo_width()}x210")
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
        self.after(0, lambda: self.level_canvas.coords(self.level_bar, 0, 0, bar_width, 8))

    def _show_history_window(self):
        hw = tk.Toplevel(self)
        hw.title("Call Transcript History")
        hw.geometry("700x450")
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
            text_area.insert(tk.END, "No speech history recorded yet in this call.")
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
            log_path = os.path.join(os.path.expanduser("~"), "Desktop", f"call_transcript_{int(time.time())}.txt")
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

