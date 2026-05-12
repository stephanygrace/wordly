# Wordly

**Wordly** is a desktop sermon highlight production tool for church media teams. It helps editors produce **vertical (9:16) sermon reels** for social media by automating repetitive steps—download, trim, verse overlay, background music mix, and export—while keeping **timing, verse copy, music, and levels** under manual control.

This is **not** a full nonlinear editor (like Premiere or DaVinci Resolve). It is a **template-based, semi-automated** pipeline built around FFmpeg.

## Features (MVP)

- Download sermon video from a **Facebook / Facebook Live** URL (`yt-dlp`); the **last URL** you used is restored on the next launch (saved when you start a download or quit the app)
- **Open a local video** (or drag-drop onto the window) when download is not possible
- **Optional yt-dlp cookies** — browse to a **Netscape cookies.txt** for Facebook logins; path is remembered when the file still exists
- **Start / end** timecodes with **±1s** and **±5s** nudges; **Set end → file end** fills the end field from the probed media length (enabled once duration is known)
- **Embedded preview** (play, pause, seek; loops playback inside the trim window while timing is valid; **timecode** shows current position / duration)
- **Media duration** hint and automatic **clamp** of start/end to the file length when you load a sermon
- **Reel layout templates** (`templates/*.json`) — pick **Built-in default** or a JSON template (resolution, top/middle/bottom heights, CRF, overlay alpha); your **last template** is restored on the next launch when that file still exists
- **Save trimmed clip…** — export only the sermon trim to an **MP4** (saved under `clips/` by default, or a path you choose)
- **Cancel** — stop an in-progress **download** (yt-dlp), **reel export**, **clip save**, or **Whisper** transcription (FFmpeg / Whisper jobs are terminated; a cancelled download may leave partial files in `downloads/`). **Esc** does the same while a job is running.
- **Menu bar** — **File**: Open sermon (Ctrl+O), **Reopen last sermon**, **Copy sermon path** (Ctrl+Shift+C), **Open recent**, **Clear recent list**, **Remove incomplete downloads…**, open exports/clips/downloads folders, **Quit** (Ctrl+Q). **Tools**: **Transcribe sermon to SRT (Whisper)…** (optional CLI). **View**: reset window/splitter layout. **Help**: About.
- **Recent sermons** — last opened paths (files that still exist) under **File → Open recent**.
- **Status bar** — current sermon name (full path in tooltip); on startup, **ffmpeg**, **ffprobe**, and **yt-dlp** versions (or missing-tool warnings).
- **Volume readouts** — live **%** next to sermon and piano sliders.
- **Remembered export filename** — last output stem is restored next launch; when you load a sermon, the export name is **auto-filled from the video filename** if the field is empty or still the generic default (`wordly-export`).
- **Remembered background music** — last chosen audio file is restored on startup when it still exists; **Clear** removes it for this session.
- **Remembered clip timing** — **Start** and **End** fields are restored across sessions and updated after a successful reel export, clip save, sermon load (after clamp), or when you quit the app.
- **Overwrite guard** — exporting or saving a clip asks before replacing an existing file.
- **FFmpeg errors** — failed renders show a short summary; **Show Details…** contains the last log lines from FFmpeg stderr.
- **Open exports folder** / **Open clips folder** / **Open downloads folder** — shortcuts in the Export section
- **Sermon-only video** (no audio track): export still works using a silent sermon bed mixed with your music
- **Bible verse overlay** (reference + text) in the top section of the vertical frame; **verse reference and text** are remembered across sessions (saved when you quit, export a reel, or save a clip)
- **Optional SubRip (.srt), WebVTT (.vtt), or ASS/SSA (.ass / .ssa) burn-in** on **Export reel** — cues are **time-shifted** to match your Start/End trim (timed to the **full** sermon). **.vtt** is converted to SRT internally (simple tags stripped); **.ass** keeps styles and karaoke tags for libass (no `force_style` override). SRT/VTT use a forced bottom style; place fonts in `assets/fonts/` for consistent rendering (same as verse overlay).
- **Background instrumental** (local audio file) with independent **sermon** and **music** volume and optional **fade in / out** on the bed
- Export **1080×1920**, **H.264** video + **AAC** audio to `exports/`

### MVP checklist (shipping)

All of the following are implemented in this repo:

| Goal | Done |
|------|------|
| Download sermon (Facebook-oriented URL) | Yes (`yt-dlp` → `downloads/`) |
| Open local / drag-drop sermon | Yes |
| Trim with start/end, nudges, duration clamp, preview loop | Yes |
| Verse overlay + template-based 9:16 layout | Yes |
| Piano bed, sermon/music levels, fades | Yes |
| Optional `.srt` / `.vtt` / `.ass` burn-in (VTT→SRT; ASS trim via pysubs2) | Yes |
| Export vertical reel + save trimmed clip + cancel (download / encode) + overwrite guard | Yes |
| Tooling status + remembered paths/stem/URL/template/verse/cookies + menus | Yes |
| Unit tests (`tests/`, stdlib **unittest** + optional **pysubs2** ASS test) | Yes |

Future ideas (not in this repo): shared **cloud** uploads, in-app **AI** drafting, bundled **Whisper** models (without the external CLI).

## Tech stack

| Area | Technology |
|------|------------|
| Language | **Python 3.12+** |
| UI | **PySide6** (Qt 6) |
| Preview | **Qt Multimedia** (`QMediaPlayer`, `QVideoWidget`) |
| Download | **yt-dlp** |
| Render | **FFmpeg** (subprocess; `libx264`, `aac`) |

## Development roadmap (phases)

| Phase | Focus | Status in this repo |
|-------|--------|---------------------|
| **1** | UI shell, yt-dlp, FFmpeg integration | Done |
| **2** | Preview player, timestamps / nudges | Done |
| **3** | Verse overlay, templates, piano / sermon mix | Done |
| **4** | Vertical export, trimmed clip save, progress, cancel, overwrite guard | Done |
| **5** | Captions & advanced | **Done (captions path)** — **.srt**, **.vtt** (→SRT), **.ass/.ssa** (trim + libass, styles/karaoke tags preserved), optional **Whisper CLI**, **cookies.txt**, incomplete-download cleanup, **Ctrl+E**. **Not in app:** AI features, cloud upload, non-CLI Whisper bundling. |

## Prerequisites

1. **Python 3.12 or newer**
2. **FFmpeg** and **ffprobe** on your system `PATH` (Wordly uses ffprobe for duration and audio detection)
3. **yt-dlp** — pulled in via `requirements.txt`; the status bar shows the importable **version** (needed for **Download**).
4. **Internet access** when using **Download** (yt-dlp)

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

Dependencies are listed in `requirements.txt` (PySide6, yt-dlp, **pysubs2** for ASS/SSA). The first line notes the **Python 3.12+** requirement.

### Running tests

From the project root (no GUI required):

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

(`test_ass_clip` runs when **pysubs2** is installed from `requirements.txt`.)

### Optional: Whisper CLI

**Tools → Transcribe sermon to SRT (Whisper)…** runs the **`whisper`** command when it is on your `PATH` (`pip install openai-whisper`). It writes `<sermon_stem>_whisper.srt` next to the sermon and loads it for optional burn-in. FFmpeg is required for decoding. Large models and long sermons can be slow; use **Cancel** or **Esc** to stop.

## How to run

From the project root (with the virtual environment activated):

```bash
python main.py
```

If your shell only has `python3`, use `python3 main.py` after activating the venv.

On first launch, Wordly creates working folders: `downloads/`, `exports/`, `clips/`, `temp/`, `templates/`, `assets/` (including `assets/fonts/`, `assets/music/`, etc.) if they are missing.

## Layout templates

JSON files in `templates/` describe the vertical frame (see `templates/default.json`). Add new `*.json` files and use **Reload list** next to the template dropdown to pick them up without restarting.

## Trimmed clips vs full reel

| Action | Output |
|--------|--------|
| **Export reel** | 1080×1920 vertical MP4 with verse overlay + mixed audio → `exports/` |
| **Save trimmed clip…** | Trimmed sermon only (H.264 + AAC if the sermon has audio) → path you choose (default suggestion under `clips/`). **No** verse overlay, music mix, or subtitle burn-in. |

## Subtitles vs reel export

| | **Export reel** | **Save trimmed clip…** |
|--|-----------------|-------------------------|
| Verse + vertical layout | Yes | No |
| Piano / sermon mix | Yes | No |
| Optional `.srt` / `.vtt` / `.ass` burn-in | Yes (trim-aligned; VTT→SRT; ASS via pysubs2) | No |

## Timecode format

**Start** and **End** fields accept the same formats FFmpeg-minded editors expect:

- **`HH:MM:SS`** or **`HH:MM:SS.mmm`** (hours, minutes, seconds, optional milliseconds)
- **`MM:SS`** or **`MM:SS.mmm`** — interpreted as **minutes:seconds** when you omit the hour segment

End must be **greater than** start. Invalid or empty values are rejected when you export, save a clip, or sync the preview trim.

## Long-running jobs and Cancel

**Download** runs **yt-dlp** on a worker thread; **Export reel** and **Save trimmed clip…** spawn **FFmpeg**; **Whisper** runs the optional **`whisper`** CLI. Progress appears in the main progress bar. Use **Cancel** or **Esc** to stop the current job: yt-dlp is interrupted cooperatively (via `DownloadCancelled`), FFmpeg encode processes are **terminated**, and Whisper is **killed**. A cancelled run may leave a **partial** file on disk (`.part` fragments in `downloads/`, or a truncated export). The **overwrite** prompt still applies the next time you target the same path.

## Keyboard shortcuts

| Shortcut | Action |
|----------|--------|
| **Ctrl+O** | Open sermon video… |
| **Enter** | In the **Facebook URL** field, starts **Download** (same as the Download button). |
| **Ctrl+Shift+C** | Copy the current sermon’s full file path to the clipboard (status bar confirms; if nothing is loaded, you get a short reminder). |
| **Ctrl+Q** | Quit Wordly |
| **Ctrl+E** | **Export reel** (same checks as the Export button — sermon, music, timing, etc.). |
| **Esc** | Cancel the current **download**, **reel export**, **clip save**, or **Whisper** run (only while that job is running; the shortcut is inactive when idle). |
| **Space** | Play / pause preview — when focus is in the **preview** panel (click the video, the **Play/Pause** button, or the seek bar first). Clicking the video also toggles playback. |

Menu-only actions (no default shortcut): **File → Open recent**, **Clear recent list**, **Remove incomplete downloads…**, **View → Reset window layout**, **Tools → Transcribe sermon to SRT (Whisper)…**, folder shortcuts under **File**, and **Help → About**.

## Tips

- **Drag and drop** a sermon video (`.mp4`, `.mov`, `.mkv`, `.webm`, `.m4v`) onto the main window to open it.
- With the URL field focused, press **Enter** to **Download** (same as the Download button).
- Under the preview, the **timecode** line shows **playback position / total duration** so you can line up edits without guessing from the seek bar alone.
- When ffprobe reports a length, **Set end → file end** fills the **End** field with the last frame time — handy for “use the whole file after this start” or resetting a bad end time after a re-download.
- **Jump to clip start / end** in the preview; use the **Play / Pause** button or **Space** when the preview panel is focused (see **Keyboard shortcuts**).
- If the window or splitter feels wrong after moving things around, use **View → Reset window layout** to clear saved geometry from `QSettings` (your sermon path and export stem are not reset).
- **Window size**, **splitter**, and last-used folders (sermon open, music browse, clip save) are remembered between sessions (Qt `QSettings`). The main window has a **minimum size** so the controls column and preview stay usable on smaller displays.
- After install, glance at the **status bar** on startup: it summarizes **ffmpeg**, **ffprobe**, and **yt-dlp** so you know download and export will work.
- While a **download** or **FFmpeg** job runs, most controls are **locked** so settings cannot drift mid-job; **Cancel** stays available for **download**, **reel export**, and **clip save**.
- If you **Cancel** a job, treat the output path (or `downloads/` folder) as **maybe incomplete** until you run again (see **Long-running jobs and Cancel**).

## Display / scaling

Wordly sets Qt’s high-DPI scale factor rounding to **PassThrough** before the UI starts, which helps avoid blurry or mis-sized widgets on **fractional display scaling** (common on Windows laptops and some external monitors). If layout still looks wrong, check the OS display scale settings and Qt’s platform notes for your setup.

The app uses Qt’s **Fusion** style for predictable controls across Windows and Linux; your system theme still applies at the window-manager level.

## Saved preferences (QSettings)

Preferences are stored under Qt organization **Wordly** / application **Wordly** (see `utils/app_settings.py`). Wordly remembers:

| What | Purpose |
|------|---------|
| Main window **geometry** | Size and position |
| **Splitter** state | Relative width of preview vs controls |
| **Last folders** | Parent directory for sermon open dialog, music file browse, optional subtitle browse (**.srt / .vtt / .ass**), and “Save trimmed clip…” |
| **Last sermon file** | Full path for **File → Reopen last sermon** (updated whenever you load a sermon) |
| **Last Facebook URL** | Text in the sermon URL field (saved when you start **Download** or quit; cleared from prefs if you empty the field and quit) |
| **Last yt-dlp cookies file** | Optional **Netscape cookies.txt** path for authenticated downloads (restored when the file still exists) |
| **Recent sermons** | Short list of file paths for **File → Open recent** |
| **Last export stem** | Default output name field on next launch |
| **Last Start / End text** | Clip timing fields restored on next launch (also saved when you close the app, export a reel, or save a clip) |
| **Last subtitle file** | Optional `.srt`, `.vtt`, or `.ass` / `.ssa` path restored when the file still exists |
| **Last music file** | Background audio path restored when the file still exists |
| **Last layout template** | Path to the last **JSON** template used for export (restored when the file still exists; otherwise **Built-in default**) |
| **Last verse reference / body** | Verse overlay fields (body capped for storage) saved when you quit, export a reel, or save a clip |

**View → Reset window layout** clears **only** saved window **geometry** and **splitter** widths. Other preferences (recent sermons, folders, export stem, trim times, music, subtitles, template, verse text) are unchanged.

## Typical workflow

1. Paste a **Facebook sermon URL** and click **Download**, or use **Open local file…** for a video you already have.
2. Set **Start** and **End** times (e.g. `01:25:00` to `01:27:00`); use the nudge buttons to fine-tune. If you need the sermon through the last frame, use **Set end → file end** once duration appears.
3. Preview the clip; use **Play** / **Pause**, the seek bar, and the **position / duration** readout under the preview.
4. Enter **verse reference** and **verse text**; optionally choose **SubRip (.srt)**, **WebVTT (.vtt)**, or **ASS/SSA** for burned-in captions on the reel (not used for “Save trimmed clip…”).
5. Choose a **background MP3** (or WAV/M4A via the file dialog).
6. Adjust **sermon** and **piano** volumes and optional **fade in/out** on the bed.
7. Optionally pick a **layout template** (or stay on **Built-in default**).
8. Enter an **output filename** (without extension) and click **Export reel**, or use **Save trimmed clip…** for a horizontal trim only (no vertical reel, no music).

## Optional: verse overlay font

If FFmpeg **drawtext** fails because no font is found, place a **`.ttf`** or **`.otf`** file in `assets/fonts/`. Wordly uses the first font file it finds there for overlays.

## Facebook downloads and cookies

Some Facebook URLs require authentication. Wordly can pass a **Netscape-format `cookies.txt`** to yt-dlp:

1. Export cookies from your browser with a trusted extension or tool (same file format yt-dlp expects).
2. Under **Sermon source**, click **Cookies…** and choose that file (optional **Clear** removes it).
3. The path is saved in preferences when the file still exists.

If download still fails, use **Open local file…** after saving the video another way. See also the [yt-dlp README](https://github.com/yt-dlp/yt-dlp#readme) for advanced options not exposed in the UI.

Use **File → Remove incomplete downloads…** to delete common yt-dlp fragment files (`*.part`, `*.ytdl`, etc.) in `downloads/` after a cancelled or failed download.

## Project layout

```
wordly/
├── main.py              # Application entry
├── requirements.txt
├── assets/              # Fonts, logos, overlays, default music (optional)
├── downloads/           # yt-dlp output
├── clips/               # Default location for “Save trimmed clip…” suggestions
├── exports/             # Final vertical reel MP4s
├── temp/                # Temporary overlay text files during export
├── templates/           # Reel layout JSON (see default.json)
├── services/            # downloader, trimmer, audio_mixer, renderer, whisper_srt
├── ui/                  # main_window, controls_panel, preview_player
├── tests/               # unittest modules (srt_clip, vtt_to_srt, trimmer)
└── utils/               # paths, timecode, layout_template, ffmpeg_progress, app_settings, recent_sermons, srt_clip, vtt_to_srt, ass_clip, downloads_cleanup, tool_versions
```
