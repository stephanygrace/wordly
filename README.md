# Wordly

Desktop wizard for church media teams: download a Facebook sermon (or open a local file), trim multiple highlights, pick a Bible verse and instrumental by theme, and export a **Filmora 15.5.3** `.wfp` project with separate editable tracks.

## Workflow

1. **Download** — paste a Facebook URL, or open a local sermon video
2. **Timestamps** — add one or more start/end ranges
3. **Preview** — scrub each segment
4. **Trim clips** — FFmpeg exports each range as its own file (`Clip001.mp4`, `Clip002.mp4`, …)
5. **Bible verse** — enter a theme; AI suggests verses (offline fallback without API key)
6. **Instrumental** — AI suggests beds; download the one you pick
7. **Layers** — review sermon / verse / music tracks
8. **Project name**
9. **Export** — writes `exports/<name>/<name>.wfp` for Filmora 15.5.3

## Requirements

- **Python 3.12+**
- **FFmpeg / ffprobe** on `PATH` (required for preview, trim, and export)
- **Filmora 15.5.3** to open the exported project (build `15.5.3.11417` or compatible)
- **Optional:** [aria2c](https://aria2.github.io/) on `PATH` — faster Facebook downloads when detected (falls back to yt-dlp’s built-in downloader)
- **Optional:** `OPENAI_API_KEY` — smarter verse and instrumental suggestions

### Install FFmpeg

| Platform | Command |
|----------|---------|
| **macOS** | `brew install ffmpeg` |
| **Windows** | `winget install Gyan.FFmpeg` or `choco install ffmpeg`, or download from [ffmpeg.org](https://ffmpeg.org/download.html) |
| **Linux / WSL** | `sudo apt install ffmpeg` |

### Install aria2c (optional, faster downloads)

| Platform | Command |
|----------|---------|
| **macOS** | `brew install aria2` |
| **Windows** | `winget install aria2.aria2` or `choco install aria2` |
| **Linux / WSL** | `sudo apt install aria2` |

Wordly detects aria2c automatically at launch and shows the active download backend on step 1.

---

## Install

### macOS

```bash
git clone <repo-url> wordly
cd wordly
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

If `python3` is missing or older than 3.12, install Python from [python.org](https://www.python.org/downloads/) or `brew install python@3.12`.

### Windows (PowerShell)

```powershell
git clone <repo-url> wordly
cd wordly
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If activation fails with *“running scripts is disabled on this system”*, either allow scripts for your user (one time):

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

or skip activation and use the venv Python directly:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### Windows (cmd)

```bat
git clone <repo-url> wordly
cd wordly
python -m venv .venv
.venv\Scripts\activate.bat
pip install -r requirements.txt
```

### Linux / WSL

Same as macOS (`source .venv/bin/activate`). On WSL, run Wordly inside Linux and open the exported `.wfp` in **Filmora on Windows** — media paths are written as `\\wsl$\...` UNC paths when needed.

---

## Run

### macOS / Linux / WSL

```bash
cd wordly
source .venv/bin/activate
python main.py
```

### Windows (PowerShell, with venv activated)

```powershell
cd wordly
.\.venv\Scripts\Activate.ps1
python main.py
```

### Windows (without activating the venv)

```powershell
cd path\to\wordly
.\.venv\Scripts\python.exe main.py
```

On first launch, Wordly creates `downloads/`, `clips/`, `exports/`, and `temp/` if they are missing.

### Optional environment variables

**macOS / Linux / WSL:**

```bash
export OPENAI_API_KEY=sk-...
export WORDLY_OPENAI_MODEL=gpt-4o-mini   # optional model override
export WORDLY_FILMORA_TEMPLATE=/path/to/custom.wfp   # optional template override
```

**Windows (PowerShell):**

```powershell
$env:OPENAI_API_KEY = "sk-..."
$env:WORDLY_OPENAI_MODEL = "gpt-4o-mini"
$env:WORDLY_FILMORA_TEMPLATE = "C:\path\to\custom.wfp"
```

---

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
│       ├── sermon-highlights.wfp   # layout reference (save from Filmora 15.5.3)
│       ├── video.mp4, music.mp3, image.jpg  # optional bundle media beside template
│       └── reference_verse_*.json  # verse/timeline reference snippets
├── downloads/           # downloaded sermon sources
├── clips/               # trimmed segments (Clip001.mp4, Clip002.mp4, …)
├── exports/             # one folder per project: .wfp + media/ + verse.txt
└── tests/
```

---

## Filmora export

Wordly clones `assets/filmora_templates/sermon-highlights.wfp` and patches paths, hashes, and timeline trims for your sermon segments, instrumental, and verse text.

The bundled template is saved from **Filmora 15.5.3 on macOS** (`project_editor_create_version: 15.5.3.11417`). If you replace it, save a blank project from your installed Filmora into `assets/filmora_templates/sermon-highlights.wfp` using the same resolution and aspect ratio you want in exports (default template: **1080×1920**, 9:16).

**Export steps**

1. Complete the wizard through **Project name**.
2. Click **Finish** (or **Generate .wfp** on the last step).
3. Wordly writes `exports/<project-name>/<project-name>.wfp` and copies media into `exports/<project-name>/media/`.
4. Open the `.wfp` from that folder in Filmora — keep the `media/` folder beside it.

**Platform notes**

| Where Wordly runs | Where Filmora runs | Path format in `.wfp` |
|-------------------|--------------------|-------------------------|
| macOS | macOS Filmora 15.5.3 | Absolute Mac paths (`file:///Users/...`) |
| Windows | Windows Filmora | `C:\...` drive paths |
| WSL | Windows Filmora | `\\wsl$\<distro>\...` UNC paths |

**Template maintenance**

- Optional bundle media beside the template: `video.mp4`, `music.mp3`, `image.jpg` (paths inside the `.wfp` should point at those files on your machine).
- After editing the template, you can sanitize leftover paths:

  ```bash
  python tools/sanitize_filmora_template.py
  ```

- Verify an export:

  ```bash
  python tools/scan_wfp_refs.py exports/your-project/your-project.wfp
  python tools/verify_export.py exports/your-project/your-project.wfp
  ```

A clean export should report `timeline JSON: OK` and no stale template path hits from `scan_wfp_refs.py`.

**Do not** open `assets/filmora_templates/sermon-highlights.wfp` for editing during export — use **Finish** and open the copy under `exports/<project>/`.

---

## Tests

**macOS / Linux / WSL:**

```bash
source .venv/bin/activate
python -m unittest discover -s tests -p 'test_*.py' -v
```

**Windows (PowerShell):**

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
```

With pytest installed:

```bash
python -m pytest tests/ -v
```
