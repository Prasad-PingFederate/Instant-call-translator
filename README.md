# 🌐 Instant Call Translator (English $\rightarrow$ Telugu & Indian Languages)

A lightweight, real-time floating subtitle HUD overlay designed for non-native English speakers during client calls on **Microsoft Teams**, **Zoom**, **Google Meet**, or any web browser call.

---

## 🌟 Why This Exists

During client meetings, speech can often be fast, accented, or filled with technical terminology. Default Windows Live Captions lacks support for regional Indian languages like Telugu.

**Instant Call Translator** captures the incoming client voice directly from your PC speakers/headphones and provides live, high-contrast, translated subtitles in **Telugu (తెలుగు)** (or Hindi, Tamil, Kannada, Marathi, etc.) with minimal latency.

---

## 🚀 Key Features

1. **System Audio Loopback (Speakers)**:
   - Captures only what comes out of your PC speakers/headphones (the client's voice).
   - Does **not** capture your own microphone by default, avoiding self-echo or screen clutter.
2. **Real-Time Translation**:
   - Transcribes spoken English using Google Speech API.
   - Translates into natural, fluent Telugu using high-speed translation with automatic fallback.
3. **Always-On-Top Floating HUD**:
   - Draggable transparent window that sits comfortably over Teams, Zoom, or your browser.
   - Large bold Telugu text rendered in native `Nirmala UI` font.
   - Compact mode (`─`) to minimize the HUD and show only Telugu text.
4. **Call History & Save Transcript**:
   - Click `📜 History` to review the full timeline of the meeting.
   - One-click export to a `.txt` file on your Desktop.
5. **Testing Mode**:
   - Toggle audio source to `🎙️ Audio: My Microphone (Testing)` so you can test it yourself before client calls.

---

## 💻 Quick Start

### 1. Installation

Clone the repository and install dependencies:

```bash
git clone https://github.com/Prasad-PingFederate/Instant-call-translator.git
cd Instant-call-translator
pip install -r requirements.txt
```

### 2. Launching

Double-click `Start_Telugu_Call_Translator.bat` or run:

```bash
python call_translator.py
```

---

## 🌐 Supported Target Languages

- **Telugu (తెలుగు)** - *Default*
- Hindi (हिन्दी)
- Tamil (தமிழ்)
- Kannada (ಕನ್ನಡ)
- Marathi (मराठी)
- Malayalam (മലയാളം)
- Gujarati (ગુજરાતી)
- Bengali (বাংলা)
- Original English

---

## 🛠️ Architecture

```
[Client speaks English in Teams/Zoom] 
              ↓
[System Audio Loopback (WASAPI / soundcard)]
              ↓
[Streaming STT (Google Speech API)]
              ↓
[Translation Engine (Google GTX / MyMemory)]
              ↓
[Transparent Floating Subtitle HUD on Screen]
```

---

## 📄 License

MIT License. Free to use and customize.

