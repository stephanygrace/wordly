# Wordly

Desktop wizard for church media teams: download a Facebook sermon, trim multiple highlights, pick a Bible verse and instrumental by theme, and export a **Filmora 14.2.9** `.wfp` project with separate editable tracks.

## Workflow

1. **Download** — paste a Facebook URL (yt-dlp with 16 parallel fragments)
2. **Timestamps** — add one or more start/end ranges
3. **Preview** — scrub each segment
4. **Trim & join** — FFmpeg trims and concatenates highlights
5. **Bible verse** — enter a theme; AI suggests verses (offline fallback without API key)
6. **Instrumental** — AI suggests beds; download the one you pick
7. **Layers** — review sermon / verse / music tracks
8. **Project name**
9. **Export** — writes `exports/<name>.wfp` for Filmora 14.2.9

## Requirements

- Python 3.12+
- FFmpeg / ffprobe on `PATH`
- **Filmora 14.2.9** to open the exported project
- Optional: `OPENAI_API_KEY` (smarter verse/music suggestions)

### Install FFmpeg

- **Windows:** [ffmpeg.org](https://ffmpeg.org/download.html) or `choco install ffmpeg`
- **macOS:** `brew install ffmpeg`
- **Linux / WSL:** `sudo apt install ffmpeg`

## Install

From the project root:

```bash
git clone <repo-url> wordly
cd wordly
python3 -m venv .venv
```

**Linux / macOS / WSL:**

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

**Windows (PowerShell):**

```powershell
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If activation fails with *“running scripts is disabled on this system”*, either allow scripts for your user (one time):

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

or skip activation and call the venv Python directly (works for install and run):

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**Windows (cmd):**

```bat
.venv\Scripts\activate.bat
pip install -r requirements.txt
```

## Run

Activate the virtual environment (if you use one), then:

```bash
python main.py
```

**Windows without activating:**

```powershell
cd path\to\wordly
.\.venv\Scripts\python.exe main.py
```

If your system only has `python3`:

```bash
python3 main.py
```

On first launch, Wordly creates `downloads/`, `clips/`, `exports/`, and `temp/` if they are missing.

### Optional environment variables

```bash
export OPENAI_API_KEY=sk-...          # smarter verse / instrumental suggestions
export WORDLY_OPENAI_MODEL=gpt-4o-mini  # optional model override
```

## Project layout

```
wordly/
├── main.py
├── models/              # wizard project state
├── services/            # download, trim, AI, Filmora .wfp export
├── ui/                  # wizard_window, preview_player
├── utils/
├── assets/
│   └── filmora_templates/
│       ├── sermon-highlights.wfp   # GUI layout reference (preferred)
│       ├── video.mp4, music.mp3, image.jpg  # media for that template
│       └── reference_media/        # tiny placeholders if bundle media missing
├── downloads/           # sermon sources
├── clips/               # trimmed / joined segments
├── exports/             # one folder per project: .wfp + media/ + verse.txt
└── tests/
```

## Filmora export

Wordly clones `assets/filmora_templates/sermon-highlights.wfp` (or `filmora_14_2_9.wfp`) and patches paths, hashes, and timeline trims for your sermon segments, joined highlights, instrumental, and verse. Media paths are written for **Windows Filmora** (`C:\...` or `\\wsl$\...` when running in WSL).

**Important**

- Use **Filmora 14.2.9** (same build as the template).
- In the wizard, click **Finish** (or **Generate .wfp**). Wordly writes `exports/<project-name>/<project-name>.wfp` and copies your joined clip, music, and verse text into `exports/<project-name>/media/`. Open the `.wfp` from that folder in Filmora — keep the `media/` folder beside it.
- The timeline keeps Filmora’s native layer stack: **title** (Bible verse), **mirrored sermon segments** on two video tracks (one muted like your GSM template), **joined highlights** reel, and **instrumental** audio.
- Put your working Filmora project in `assets/filmora_templates/` as `**sermon-highlights.wfp`** with `**video.mp4**`, `**music.mp3**`, and `**image.jpg**` in the same folder (paths inside the `.wfp` should point at those files on this PC).
- After updating the template, run once: `python tools/sanitize_filmora_template.py` (rewrites leftover Downloads/GSM paths to the bundle media).
- **Do not** open the template `.wfp` for editing in Filmora from Wordly’s export step — use **Finish** and open the copy under `exports/<project>/`.

To repoint the bundled template at local placeholder media (only if you are not using your own GSM project as the template):

```powershell
.\.venv\Scripts\python.exe tools\sanitize_filmora_template.py
```

Check an export for leftover template paths:

```powershell
.\.venv\Scripts\python.exe tools\scan_wfp_refs.py exports\your-project.wfp
.\.venv\Scripts\python.exe tools\verify_export.py exports\your-project.wfp
```

A clean export should report `timeline JSON: OK` and no `Facebook_1` / `D:/` hits from `scan_wfp_refs.py`.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
```

On Linux / macOS / WSL with the venv activated:

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

