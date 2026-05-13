# Wordly

Desktop wizard for church media teams: download a Facebook sermon, trim multiple highlights, pick a Bible verse and instrumental by theme, and export a **Filmora 14.2.9** `.wfp` project with separate editable tracks.

## Workflow

1. **Download** — paste a Facebook URL (fast yt-dlp + aria2c, or Internet Download Manager on Windows)
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
- Optional: `aria2c` (faster downloads), `OPENAI_API_KEY` (smarter verse/music suggestions)

### Install FFmpeg

- **Windows:** [ffmpeg.org](https://ffmpeg.org/download.html) or `choco install ffmpeg`
- **macOS:** `brew install ffmpeg`
- **Linux / WSL:** `sudo apt install ffmpeg`

Optional faster downloads: `sudo apt install aria2` (Linux/WSL) or install aria2c on Windows.

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

**Windows (cmd):**

```bat
.venv\Scripts\activate
pip install -r requirements.txt
```

## Run

Activate the virtual environment, then:

```bash
python main.py
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
│   └── filmora_templates/filmora_14_2_9.wfp   # Filmora 14.2.9 reference project
├── downloads/           # sermon sources
├── clips/               # trimmed / joined segments
├── exports/             # .wfp output
└── tests/
```

## Filmora export

Wordly clones `assets/filmora_templates/filmora_14_2_9.wfp` and patches media paths for your joined highlight clip and instrumental. Media paths are written for **Windows Filmora** (`C:\...` or `\\wsl$\...` when running in WSL).

Replace the template with your own blank Filmora 14.2.9 project if needed. Inspect any `.wfp`:

```bash
python tools/inspect_wfp.py path/to/project.wfp
```

## Tests

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```
