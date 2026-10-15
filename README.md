# Wordly

Desktop wizard for church media teams: download a Facebook sermon (or open a local file), trim multiple highlights, pick a Bible verse and instrumental, and export a **DaVinci Resolve Free** import bundle (FCPXML + media) — no Filmora, no paid Studio scripting for the happy path.

## Workflow

1. **Download** — paste a Facebook URL, or open a local sermon video
2. **Timestamps** — add one or more start/end ranges
3. **Preview** — scrub each segment; Next exports `Clip001.mp4`, …
4. **Verse & music** — enter the verse; download or choose an instrumental
5. **Project** — name the export and **Generate Resolve project**

## Requirements

- **Python 3.12+**
- **FFmpeg / ffprobe** on `PATH`
- **DaVinci Resolve Free** to import the FCPXML and Deliver an MP4
- **Optional:** [aria2c](https://aria2.github.io/) — faster Facebook downloads when detected
- **Optional:** `OPENAI_API_KEY` — smarter verse and instrumental suggestions

### Install FFmpeg

| Platform | Command |
|----------|---------|
| **macOS** | `brew install ffmpeg` |
| **Windows** | `winget install Gyan.FFmpeg` or `choco install ffmpeg` |
| **Linux / WSL** | `sudo apt install ffmpeg` |

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

### Windows / Linux

Same venv + `pip install -r requirements.txt` pattern as usual. **v1 export paths are macOS-first** (absolute `file://` URLs). Windows/WSL can still run the wizard; import the bundle into Resolve on the host carefully.

---

## Run

```bash
cd wordly
source .venv/bin/activate
python main.py
```

On first launch, Wordly creates `downloads/`, `clips/`, `exports/`, and `temp/` if they are missing.

### Optional environment variables

```bash
export OPENAI_API_KEY=sk-...
export WORDLY_OPENAI_MODEL=gpt-4o-mini   # optional model override
export WORDLY_RESOLVE_TEMPLATE=/path/to/custom.fcpxml   # optional template override
```

---

## Project layout

```
wordly/
├── main.py
├── models/                 # wizard project state
├── services/               # download, trim, AI, Resolve FCPXML export
├── ui/                     # wizard_window, preview_player
├── utils/
├── assets/
│   └── resolve_templates/
│       └── sermon-highlights.fcpxml   # Free-compatible interchange marker
├── downloads/
├── clips/
├── exports/                # one folder per project: .fcpxml + media/ + verse.srt
├── docs/
│   ├── WORDLY_RESOLVE_FREE_SPEC.md
│   └── TEMPLATE_AUTHORING.md
└── tests/
```

---

## Resolve export

Wordly writes `exports/<name>/`:

| File | Role |
|------|------|
| `<name>.fcpxml` | Timeline: clips end-to-end + instrumental (import via Resolve) |
| `media/Clip00N.*` | Stable clip filenames |
| `media/instrumental.*` | Instrumental bed |
| `verse.srt` / `verse.txt` | On-screen verse (import subtitle or copy into Text+) |
| `OPEN_IN_RESOLVE.txt` | Short import / Deliver steps |
| `wordly_manifest.json` | Machine-readable export metadata |

**Export steps**

1. Complete the wizard through **Project**.
2. Click **Generate Resolve project**.
3. Wordly reveals the folder and launches Resolve when installed.
4. In Resolve: **File → Import → Timeline…** → select the `.fcpxml`.
5. Import `verse.srt` if needed → **Deliver** H.264 1080×1920.

Resolve Free does **not** watermark normal exports. Do not add Studio-only effects to the Wordly timeline on Free.

Template authoring: [docs/TEMPLATE_AUTHORING.md](docs/TEMPLATE_AUTHORING.md). Full product/architecture: [docs/WORDLY_RESOLVE_FREE_SPEC.md](docs/WORDLY_RESOLVE_FREE_SPEC.md).

Verify:

```bash
python tools/verify_export.py exports/your-project/your-project.fcpxml
```

---

## Tests

```bash
source .venv/bin/activate
python -m unittest discover -s tests -p 'test_*.py' -v
```
