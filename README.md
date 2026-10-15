# Wordly

Desktop wizard for church media teams: download a Facebook or YouTube sermon (or open a local file), let Claude find the highlights or trim them by hand, add captions, pick a Bible verse and instrumental, and export a vertical sermon reel as a **DaVinci Resolve Free** import bundle (FCPXML + media).

## Workflow

1. **Download** — paste a Facebook or YouTube URL, or open a local sermon video
2. **Timestamps** — add start/end ranges, or click **Find highlights with Claude** to transcribe the service and pick 2–3 highlights
3. **Preview** — scrub each segment; Next exports `Clip001.mp4`, … (VP9/AV1/HEVC sources are re-encoded so they edit smoothly)
4. **Captions** — optionally transcribe the clips with Whisper, review the text, and pick a caption style
5. **Verse & music** — enter the verse; download or choose an instrumental
6. **Project** — name the export, toggle the reel layout, and **Generate Resolve project**

## Requirements

- **Python 3.12+**
- **FFmpeg / ffprobe** on `PATH`
- **DaVinci Resolve Free** to import the FCPXML and Deliver an MP4
- **Optional:** [aria2c](https://aria2.github.io/) — faster Facebook downloads when detected
- **Optional:** `ANTHROPIC_API_KEY` — **Find highlights with Claude**
- **Optional:** `OPENAI_API_KEY` — smarter verse and instrumental suggestions

Python dependencies (`requirements.txt`) include Whisper for captions (`mlx-whisper` on Apple Silicon, `faster-whisper` elsewhere), Apple Vision for speaker tracking in the reel layout (macOS only), and the `anthropic` SDK.

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

On first launch, Wordly creates `downloads/`, `clips/`, `exports/`, and `temp/` if they are missing. It also installs the **Wordly Import** script into Resolve's **Workspace → Scripts** menu and the bundled Outfit fonts used by captions.

### Anthropic API key

Copy `.env.example` to `.env` and set `ANTHROPIC_API_KEY` (or paste the key when the wizard asks; it saves it to `.env`). Wordly also reads the environment variable and, on macOS, the Keychain. `.env` is git-ignored.

### Optional environment variables

```bash
export ANTHROPIC_WORKSPACE_ID=wrkspc_...                 # only for keys not tied to a workspace
export OPENAI_API_KEY=sk-...
export WORDLY_OPENAI_MODEL=gpt-4o-mini                  # verse/music suggestion model
export WORDLY_WHISPER_MODEL=large-v3-turbo              # caption transcription model
export WORDLY_WHISPER_LANGUAGE=tl                       # spoken language (default: tl)
export WORDLY_RESOLVE_TEMPLATE=/path/to/custom.fcpxml   # template override
```

### Customising captions

- `assets/caption_styles.json` — caption looks (font, colours, outline, highlight of the spoken word, position, line length). Copy a style, rename it, and it appears in the Captions step.
- `assets/caption_replacements.json` — spoken phrase → caption text (e.g. "first corinthians" → "1 Corinthians").

---

## Project layout

```
wordly/
├── main.py
├── models/                 # wizard project state
├── services/               # download, trim, transcribe, highlights, reel layout, Resolve export
├── ui/                     # wizard_window, preview_player
├── utils/
├── assets/
│   ├── caption_styles.json
│   ├── caption_replacements.json
│   ├── fonts/                         # Outfit (OFL)
│   ├── resolve_scripts/
│   │   └── Wordly Import.lua          # Workspace → Scripts entry
│   └── resolve_templates/
│       └── sermon-highlights.fcpxml   # Free-compatible interchange marker
├── downloads/
├── clips/
├── exports/                # one folder per project: .fcpxml + media/ + .srt files
├── tools/                  # verify_export.py, setup_windows_path.ps1
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
| `<name>.fcpxml` | 1080×1920 timeline: clips end-to-end, reel layout, verse, captions, instrumental |
| `media/Clip00N.*` | Stable clip filenames |
| `media/instrumental.*` | Instrumental bed |
| `verse.srt` / `verse.txt` | On-screen verse (import subtitle or copy into Text+) |
| `captions.srt` | Caption fallback when captions are on (import as a subtitle) |
| `OPEN_IN_RESOLVE.txt` | Short import / Deliver steps |
| `wordly_manifest.json` | Machine-readable export metadata |

**Export steps**

1. Complete the wizard through **Project**.
2. Click **Generate Resolve project**.
3. On macOS, Wordly reveals the folder, launches Resolve, and runs **Workspace → Scripts → Wordly Import** to load the timeline.
4. If that doesn't work: **File → Import → Timeline…** → select the `.fcpxml`.
5. Import `verse.srt` / `captions.srt` if needed → **Deliver** H.264 1080×1920.

With **Reel layout** on, each clip is pre-rendered with ffmpeg: the video sits full width, zoomed in on the speaker, over a blurred portrait crop of itself, with the verse above and captions below.

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
