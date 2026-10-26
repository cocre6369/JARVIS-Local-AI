# JARVIS — Local AI Desktop Assistant

A fully local, autonomous AI assistant for Windows. It sees your screen, talks to you,
controls apps for you, and never sends your data anywhere — all AI runs on your own
machine through [Ollama](https://ollama.com).

---

## ⬇️ Download (Windows 10/11, 64-bit)

### 👉 [**Get the latest release here**](https://github.com/cocre6369/JARVIS-Local-AI/releases/latest)

| File | What it is |
|---|---|
| **[JARVIS-Setup-1.0.3.exe](https://github.com/cocre6369/JARVIS-Local-AI/releases/download/v1.0.3/JARVIS-Setup-1.0.3.exe)** | **Recommended.** Double-click installer — no admin rights needed, creates desktop & Start Menu shortcuts, opens the first-run wizard. |
| [JARVIS-portable-1.0.3.zip](https://github.com/cocre6369/JARVIS-Local-AI/releases/download/v1.0.3/JARVIS-portable-1.0.3.zip) | No-install version — unzip anywhere and run `JARVIS\JARVIS.exe`. |

New releases always appear on the **[Releases page](https://github.com/cocre6369/JARVIS-Local-AI/releases)**;
the "latest release" link above automatically points at the newest one.

### After installing

1. Install [Ollama](https://ollama.com/download) (JARVIS's AI brain — free, local).
2. Pull a model, e.g. run `ollama pull qwen2.5:7b` once in a terminal.
3. Launch JARVIS — the first-run wizard finds Ollama, lists your models and sets everything up.

---

## What it does

- **Autonomous tasks** — "Open Spotify and play Violent Crimes by Kanye West": JARVIS plans,
  acts, *verifies* each step on screen, recovers from mistakes and reports back by voice.
- **Conversation** — natural voice in (local speech-to-text, "Hey JARVIS" wake word) and
  voice out (local neural TTS, interruptible).
- **Vision** — screenshots, OCR, UI Automation element matching; answers "what am I looking at?".
- **Memory** — persistent local SQLite memory you can view, edit, delete or disable.
- **Integrations** — native APIs first (Spotify SMTC, browsers, Windows), visual automation fallback.
- **Works with apps it has never seen** — it reads the screen, it doesn't rely on hardcoded scripts.

## Security you can hold it to

Enforced in application code — the AI cannot talk its way around it:

- **Prohibited outright:** uninstalling software, admin/UAC changes, registry edits,
  touching Defender/firewall, system files, account changes.
- **Always asks first (per action, never blanket):** downloads, sending messages/emails/posts,
  permanent deletes, overwrites, bulk moves, banking/financial sites, sensitive personal data —
  with what/why/consequences/reversibility explained.
- **Password fields are masked** in everything it "sees"; credentials are never stored.
- **Hard resource ceilings:** 12 GB RAM / 8 GB VRAM with continuous monitoring and load-shedding.
- **Local-only:** no cloud AI, no telemetry, no uploads. Internet is used only to fetch
  optional local models/assets — always with your permission.

## Recommended hardware

Tested target: RTX 5070 Laptop (8 GB VRAM) · i7-13650HK · 24 GB RAM · 1 TB SSD.
Anything with ~16 GB RAM and a modern GPU (or CPU-only with a small model) will run.

## Building from source

```powershell
# Windows, Python 3.11+
pip install -r requirements.txt
python -m jarvis            # run the app
python -m jarvis doctor     # diagnose your setup
.\packaging\build_windows.ps1   # build the .exe + installer locally
```

The `.exe` cannot be cross-compiled — CI builds it on a real Windows runner
(`.github/workflows/build-windows.yml`) and publishes it to Releases on every `v*` tag.

## License & status

Version 1.0.3 — first complete build + launch, loading, console, vision and tray fixes. See [Project Jarvis Design Prompt.txt](Project%20Jarvis%20Design%20Prompt.txt)
for the full design specification.
