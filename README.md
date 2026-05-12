# Wordly

**Wordly** is a desktop sermon highlight production tool for church media teams. It helps editors produce **vertical (9:16) sermon reels** for social media by automating repetitive steps—download, trim, verse overlay, background music mix, and export—while keeping **timing, verse copy, music, and levels** under manual control.

This is **not** a full nonlinear editor (like Premiere or DaVinci Resolve). It is a **template-based, semi-automated** pipeline built around FFmpeg.

## Features (MVP)

- Download sermon video from a **Facebook / Facebook Live** URL (`yt-dlp`)
- **Open a local video** if download is not possible
- **Start / end** timecodes with **±1s** and **±5s** nudges
- **Embedded preview** (play, pause, seek; optional loop inside the trim window)
- **Bible verse overlay** (reference + text) in the top section of the vertical frame
- **Background instrumental** (local audio file) with independent **sermon** and **music** volume and optional **fade in / out** on the bed
- Export **1080×1920**, **H.264** video + **AAC** audio to `exports/`

Future ideas (not in this repo scope yet): Whisper captions, ASS subtitles, AI highlight detection, cloud upload.

## Tech stack

| Area | Technology |
|------|------------|
| Language | **Python 3.12+** |
| UI | **PySide6** (Qt 6) |
| Preview | **Qt Multimedia** (`QMediaPlayer`, `QVideoWidget`) |
| Download | **yt-dlp** |
| Render | **FFmpeg** (subprocess; `libx264`, `aac`) |

## Prerequisites

1. **Python 3.12 or newer**
2. **FFmpeg** on your system `PATH` (must include `ffmpeg`; `ffprobe` is useful for future tooling)
3. **Internet access** when using **Download** (yt-dlp)

### Installing FFmpeg

- **Windows:** Install from [ffmpeg.org](https://ffmpeg.org/download.html) or a package manager (e.g. Chocolatey: `choco install ffmpeg`), then ensure `ffmpeg` is available in a terminal.
- **macOS:** `brew install ffmpeg`
- **Linux:** `sudo apt install ffmpeg` (Debian/Ubuntu) or your distro equivalent.

### Qt Multimedia / video preview

- **Windows:** Usually works out of the box with the PySide6 wheels.
- **Linux / WSL:** You may need extra GStreamer (or distro-specific) packages if the preview shows no video; rendering via FFmpeg still works if preview does not.

## Installation

Clone or copy the project, then from the project root:

```bash
python3 -m venv .venv
```

**Windows (cmd):**

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

**Windows (PowerShell) / macOS / Linux:**

```bash
source .venv/bin/activate   # Linux/macOS
# or:  .venv\Scripts\Activate.ps1   # PowerShell

pip install -r requirements.txt
```

Dependencies are listed in `requirements.txt` (PySide6, yt-dlp).

## How to run

From the project root (with the virtual environment activated):

```bash
python main.py
```

On first launch, Wordly creates working folders: `downloads/`, `exports/`, `temp/`, `assets/` (including `assets/fonts/`, `assets/music/`, etc.) if they are missing.

## Typical workflow

1. Paste a **Facebook sermon URL** and click **Download**, or use **Open local file…** for a video you already have.
2. Set **Start** and **End** times (e.g. `01:25:00` to `01:27:00`); use the nudge buttons to fine-tune.
3. Preview the clip; use **Play** / **Pause** and the seek bar.
4. Enter **verse reference** and **verse text**; choose a **background MP3** (or WAV/M4A via the file dialog).
5. Adjust **sermon** and **piano** volumes and optional **fade in/out** on the bed.
6. Enter an **output filename** (without extension) and click **Export reel**. The file is written under **`exports/`**.

## Optional: verse overlay font

If FFmpeg **drawtext** fails because no font is found, place a **`.ttf`** or **`.otf`** file in `assets/fonts/`. Wordly uses the first font file it finds there for overlays.

## Facebook downloads and cookies

Some Facebook URLs require authentication or cookies. If download fails, use **Open local file…** after saving the sermon with another tool, or configure **yt-dlp** with cookies (see [yt-dlp README](https://github.com/yt-dlp/yt-dlp#readme); cookie options are not wired in the UI in the MVP).

## Project layout

```
wordly/
├── main.py              # Application entry
├── requirements.txt
├── assets/              # Fonts, logos, overlays, default music (optional)
├── downloads/           # yt-dlp output
├── clips/               # Reserved for future clip workflows
├── exports/             # Final MP4 renders
├── temp/                # Temporary overlay text files during export
├── templates/           # Reserved for future templates
├── services/            # downloader, trimmer, audio_mixer, renderer
├── ui/                  # main_window, controls_panel, preview_player
└── utils/               # paths, timecode parsing
```
