# Wordly → DaVinci Resolve Free — Build Spec

> **Source of truth for the Resolve Free migration in this repository.**  
> Implemented in-place (no new repo). Filmora `.wfp` export is removed.

---

## 1. Product summary

**Wordly** is a desktop wizard for church media teams. It:

1. Downloads a Facebook sermon (or opens a local video).
2. Lets the user mark multiple highlight time ranges.
3. Previews those ranges.
4. Collects a Bible verse (reference + body) and an instrumental audio track.
5. Builds an **editable NLE project** (vertical 9:16 reel) with media beside it.
6. Opens that project in the NLE so the user can export an MP4.

**This repository targets DaVinci Resolve Free** (Filmora is not used).

### Non-goals

- Do **not** require DaVinci Resolve **Studio** (paid). External Python scripting that needs Studio is out of scope for v1.
- Do **not** depend on Wondershare Filmora in any form.
- Do **not** add watermarks; Resolve Free itself does not watermark normal exports.
- Do **not** auto-render the final MP4 via Studio scripting in v1 — user clicks Export/Deliver in Resolve (same UX as Filmora “open then export”).
- Do **not** implement cracked / pirated software workflows.

### Success definition (v1)

User completes the wizard on macOS (primary) → Wordly writes a project bundle → Resolve Free opens with:

- sermon segment clip(s) on video track(s),
- instrumental on an audio track,
- verse visible as styled text (from template) **or** as an importable subtitle,
- timeline set to **1080×1920 @ 30fps**,
- media online (no missing-file chase under normal use),

…then user exports MP4 from Resolve Deliver **without a watermark**.

---

## 2. Why this architecture (context)

The previous Filmora-based Wordly did **not** encode the final branded MP4. It:

- Cloned a real Filmora template (`.wfp` ZIP),
- Patched media paths, segment trims, verse text, durations/hashes,
- Wrote `exports/<name>/` with `.wfp` + `media/`,
- Launched Filmora.

**Watermark problem:** Filmora free exports stamp a large watermark. Resolve Free does **not**, as long as Studio-only effects are not used.

**Keep the same product pattern:**

> One-time human-designed **template** holds the look. Wordly **swaps media + verse + durations** for each sermon.

That is intentional product design, not a temporary hack.

---

## 3. Constraints — all free

| Component | Allowed | Forbidden |
|-----------|---------|-----------|
| DaVinci Resolve | **Free** edition | Studio-only features (Magic Mask, Super Scale, Studio noise reduction, multi-GPU encode extras, etc.) that watermark Free exports |
| External Resolve scripting API | Not required for v1 | Do not design v1 around Studio-only external scripting |
| Interchange | FCPXML / OTIO / Resolve timeline import of files Wordly generates | Reverse-engineering proprietary Filmora `.wfp` |
| Download / trim | yt-dlp, FFmpeg | Paid download APIs |
| UI | PySide6 / Qt | — |
| AI verse suggest (optional later) | Offline buckets / free bible-api | Require OpenAI for core flow |

If a design choice requires Studio, mark it `Studio-only` and provide a Free fallback.

---

## 4. Wizard UX (parity with current Wordly)

Five steps (keep this order unless UX research says otherwise):

| Step | Name | User actions | Auto behavior |
|------|------|--------------|---------------|
| 0 | Download | Paste Facebook URL **or** open local video | yt-dlp download; probe duration |
| 1 | Timestamps | Add N segments (start/end, optional label) | Validate vs duration |
| 2 | Preview | Select segment; scrub/play | On Next: FFmpeg export `Clip001.mp4`… if stale |
| 3 | Verse & music | Enter verse (line1=reference, rest=body); music URL or local file | Download music via yt-dlp into assets |
| 4 | Project | Edit project name; **Generate Resolve project** | Build bundle; open Resolve |

**Project naming default:** last Sunday as `MM.DD.YY` (helper equivalent to current `default_export_project_name`).

**Gate:** cannot generate without sermon (or clips), ≥1 segment, verse text, and local music file.

---

## 5. Domain model

```python
@dataclass
class ClipSegment:
    start_text: str   # timecode string, e.g. "00:12:34"
    end_text: str
    label: str = ""

@dataclass
class VerseChoice:
    reference: str    # e.g. "John 3:16"
    text: str         # body

@dataclass
class MusicChoice:
    title: str
    artist: str = ""
    search_query: str = ""
    local_path: Path | None = None

@dataclass
class ProjectState:
    fb_url: str = ""
    cookies_file: Path | None = None   # optional; expose in UI if FB download fails
    sermon_path: Path | None = None
    sermon_duration_s: float = 0.0
    segments: list[ClipSegment] = ...
    clip_paths: list[Path] = ...       # Clip001.mp4, Clip002.mp4, …
    joined_clip_path: Path | None = None  # often clip_paths[0] for template slot
    selected_verse: VerseChoice | None = None
    selected_music: MusicChoice | None = None
    project_name: str = ...
    resolve_output_path: Path | None = None  # primary deliverable path
```

---

## 6. Target canvas / creative layout

Match previous Filmora defaults unless the Resolve template changes them:

| Setting | Value |
|---------|-------|
| Resolution | **1080 × 1920** (9:16) |
| Frame rate | **30 fps** |
| Audio | 48 kHz, stereo |

### Template creative intent (parity)

The Filmora template `sermon-highlights` roughly provided:

1. **Sermon video** — portrait framing; may include a low-opacity “fill” layer + sharp foreground (stacked look). Replicate in Resolve with video tracks + opacity/transform — **design once in the template**, not procedurally in v1 if hard.
2. **Instrumental** — separate audio track under the picture.
3. **Verse title** — styled on-screen text for the duration of the highlight reel (or most of it). Prefer styling **in the Resolve template** (Text+ / Text) so Wordly only replaces the string content.
4. **Cover / end card** — short image near the end (previous app grabbed `cover.jpg` via FFmpeg at `ss=1`). Optional for v1 if difficult; include if the template has a placeholder still.

Wordly does **not** need a visual layout editor in v1. Layout = template.

---

## 7. Architecture overview

```
┌─────────────────────────────────────────────────────────────┐
│ Wordly Desktop (PySide6)                                    │
│  Download → Timestamps → Preview → Verse/Music → Project    │
└───────────────┬─────────────────────────────────────────────┘
                │
                ▼
┌─────────────────────────────────────────────────────────────┐
│ Media prep (editor-agnostic)                                │
│  yt-dlp, FFmpeg trim → clips/, music → assets/music/        │
│  optional cover.jpg                                         │
└───────────────┬─────────────────────────────────────────────┘
                │
                ▼
┌─────────────────────────────────────────────────────────────┐
│ Resolve export layer (NEW — replaces Filmora .wfp stack)    │
│  1. Clone / instance template                               │
│  2. Copy media into exports/<name>/media/                   │
│  3. Patch paths, durations, segment trims, verse text       │
│  4. Write sidecar: verse.txt, wordly_manifest.json, README  │
│  5. Launch Resolve / reveal folder                          │
└───────────────┬─────────────────────────────────────────────┘
                │
                ▼
         DaVinci Resolve Free  →  Deliver MP4 (user)
```

### Modules to build

| Module | Responsibility |
|--------|----------------|
| `services/downloader.py` | Facebook / generic yt-dlp download |
| `services/music_downloader.py` | Audio-only yt-dlp |
| `services/trimmer.py` / `multi_clip.py` | Probe + export Clip00N |
| `services/ffmpeg_frame.py` | Cover still |
| `services/resolve_template.py` | Locate template; env override |
| `services/resolve_export.py` | **Core:** build export bundle from template + ProjectState |
| `services/resolve_launcher.py` | Find + open Resolve / project |
| `ui/wizard_window.py` | 5-step wizard |
| `models/project.py` | State |

Reuse concepts from the Filmora Wordly codebase where useful (trim/download/UI), but **do not** copy Filmora `.wfp` generators — new export layer only.

---

## 8. Resolve Free export strategy (decide + implement)

Resolve does **not** use a simple self-contained ZIP project the way Filmora `.wfp` does. Projects live in a database; sharing uses `.drp` / archive / timeline interchange.

### Recommended v1 approach (Free-compatible)

**Hybrid template + interchange (preferred):**

1. **Human creates once** in Resolve Free a master project/timeline that looks correct (9:16, tracks, Text+ verse placeholder, music track, video slots).
2. From that, export a **timeline interchange** Wordly can regenerate reliably:
   - Prefer **OTIO** if Resolve import round-trips cleanly for your template on Free, **or**
   - Prefer **FCPXML** for clip trim + audio layout, **plus**
   - A documented Resolve **DRT / still-frame / template project archive** if needed for styled titles.
3. Wordly for each sermon:
   - Copies media into `exports/<name>/media/` with **stable filenames** (see below).
   - Emits / patches interchange so absolute paths point at that folder.
   - Writes verse into the format that survives import (see Verse handling).
4. User imports timeline (or Wordly opens a folder + instructions). Prefer “one Open” UX.

### Stable media filenames (critical)

Always rewrite clips to fixed names so the template never depends on sermon titles:

```
exports/<safe_project_name>/
  <safe_project_name>.fcpxml          # or .otio / primary timeline file
  media/
    Clip001.mp4
    Clip002.mp4
    ...
    instrumental.<ext>
    cover.jpg                         # optional
  verse.txt
  wordly_manifest.json
  OPEN_IN_RESOLVE.txt
```

Template placeholders should reference these **relative or absolute** paths after patching. Prefer absolute paths that exist on the current Mac at generate time (mirror old Filmora Mac behavior).

### Verse handling (Free)

FCPXML titles often **do not** round-trip into Resolve as editable Text+. Pick one Free approach and document it in `OPEN_IN_RESOLVE.txt`:

| Option | Pros | Cons |
|--------|------|------|
| **A. Template Text+ + patch string** | Best visual parity with Filmora | Harder; needs a patchable stored format (`.drp`/archive research) |
| **B. `.srt` / subtitle track duration = timeline** | Simple, Free-friendly | Style in Resolve once or accept subtitle look |
| **C. Burn-in ASS via FFmpeg as optional “quick MP4”** | No Resolve for verse | Loses editability of verse |

**v1 recommendation:** implement **B** always (reliable). Add **A** if a maintainable Free patch path is found. Keep **C** as optional “Export MP4 without Resolve” later — not required for Resolve migration.

### What not to assume

- Do not assume external `DaVinciResolveScript` works on Free — treat as Studio.
- Do not ship binary Filmora templates.
- Do not rely on Windows UNC / WSL paths for v1 unless you also support Windows hosts; **primary target: macOS**.

---

## 9. Open / launch Resolve (macOS)

Locate app bundles such as:

- `/Applications/DaVinci Resolve/DaVinci Resolve.app`
- `/Applications/DaVinci Resolve.app`

Behavior:

1. Reveal `exports/<name>/` in Finder.
2. If a reliably openable file exists, open it with Resolve; else show `OPEN_IN_RESOLVE.txt` with exact clicks:
   - File → Import → Timeline… (or Media Pool import)
   - Point at the generated FCPXML/OTIO
   - Confirm media online
3. Fail soft with a clear error if Resolve is not installed.

---

## 10. Template authoring guide (for humans — document in repo)

Create `docs/TEMPLATE_AUTHORING.md` covering:

1. Install Resolve Free.
2. New project: 1080×1920, 30fps.
3. Import placeholder files named exactly like export media names.
4. Build timeline: video layers, audio, Text+/titles, optional cover.
5. Export whatever Wordly’s importer expects (FCPXML/OTIO/archive).
6. Save under `assets/resolve_templates/` (name TBD, e.g. `sermon-highlights.fcpxml` + companion media stubs).
7. Override path via env `WORDLY_RESOLVE_TEMPLATE`.

Wordly must refuse generate with a helpful message if the template is missing.

---

## 11. Suggested repository layout

```
wordly/                          # or new product name
  main.py
  requirements.txt               # PySide6, yt-dlp; Python 3.12+
  README.md
  SPEC.md                        # this document (or docs/SPEC.md)
  docs/
    TEMPLATE_AUTHORING.md
  assets/
    resolve_templates/           # committed template + stub media if small
    music/                       # .gitkeep
  models/
    project.py
  services/
    downloader.py
    music_downloader.py
    trimmer.py
    multi_clip.py
    ffmpeg_frame.py
    resolve_template.py
    resolve_export.py
    resolve_launcher.py
  ui/
    wizard_window.py
    preview_player.py
    timecode_edit.py
  utils/
    paths.py                     # downloads/, clips/, exports/, temp/
    timecode.py
    export_name.py
    ffmpeg_paths.py
    ytdlp_paths.py
    app_settings.py
    console_log.py
  tools/
    verify_export.py             # validate generated interchange + media exists
    scan_template_refs.py
  tests/
    test_multi_clip.py
    test_trimmer.py
    test_downloader.py
    test_resolve_export.py       # heavy: golden fixture + patch assertions
    test_wizard_pipeline.py
```

---

## 12. Implementation phases

### Phase 0 — Scaffold

- Repo, venv, PySide6 shell, empty wizard with 5 steps.
- Path dirs: `downloads/`, `clips/`, `exports/`, `temp/`, `assets/`.
- `ProjectState` + logging.

### Phase 1 — Media pipeline (parity)

- Download (FB URL + local open).
- Timestamps UI + validation.
- Preview player (Qt + FFmpeg frame fallback).
- Trim `Clip00N.mp4`.
- Verse + music download/local.
- **Done when:** user can reach Project step with real media on disk without Resolve.

### Phase 2 — Resolve template + interchange research spike

- **Decision (v1):** FCPXML 1.9 + `verse.srt` (Option B). Wordly generates the timeline; `assets/resolve_templates/sermon-highlights.fcpxml` gates export / documents canvas.
- Manual QA still required: import generated FCPXML in Resolve Free and confirm media + Deliver.

### Phase 3 — `resolve_export.py`

- Prepare media bundle (stable names).
- Clone/patch template → `exports/<name>/`.
- Patch verse (`.srt` and/or text).
- Write manifest + `OPEN_IN_RESOLVE.txt`.
- Unit tests with a tiny fixture template (no binary NLE required for CI if XML/OTIO).

### Phase 4 — Launcher + wizard Finish

- Generate button wires to export + launch/reveal.
- UX copy: “Open in Resolve Free → Deliver → no watermark if you avoid Studio-only effects.”

### Phase 5 — Polish

- Cookie file picker if FB blocks.
- `tools/verify_export.py`.
- README: install Resolve Free, FFmpeg, run Wordly.
- Optional: dual-layer look parity if missing.
- Optional later: FFmpeg “final MP4” bypass (out of core Resolve path).

---

## 13. Testing requirements

| Area | Tests |
|------|-------|
| Timecode parse/format | unit |
| Trim windows | temp media fixtures |
| Export bundle | golden files under `tests/fixtures/resolve_template/` |
| Manifest | JSON schema-ish keys |
| Launcher | mock `subprocess` / path discovery |
| Wizard happy path | pipeline test with stubs (no GUI if heavy) |

CI should **not** require Resolve installed. Install Resolve only for manual QA of Deliver.

Manual QA checklist:

1. Generate project on Mac with 2–4 segments.
2. Import/open in Resolve Free.
3. All media online.
4. Verse readable.
5. Music present.
6. Deliver H.264 1080×1920 — **no watermark**.
7. Confirm no Studio-only FX used in template.

---

## 14. Acceptance criteria

- [ ] Full 5-step wizard works without Filmora.
- [ ] Export bundle self-contained under `exports/<name>/`.
- [ ] Resolve Free can open/import and play timeline.
- [ ] Export MP4 has no watermark with free features only.
- [ ] Template override via `WORDLY_RESOLVE_TEMPLATE`.
- [ ] Missing template fails with actionable error.
- [ ] README + TEMPLATE_AUTHORING enough for a new developer.
- [ ] No Studio API required to complete the happy path.

---

## 15. Copy / messaging (UI strings)

Replace Filmora wording:

- “Generate Filmora project” → **“Generate Resolve project”**
- “Open in Filmora” → **“Open in DaVinci Resolve”**
- Host note: Mac absolute paths; keep `media/` beside the timeline file.

Warn once: *Resolve Free exports without watermarks. Using Studio-only effects in Free can watermark the deliverable — do not add those to the Wordly template.*

---

## 16. Reference — previous Filmora Wordly (do not port blindly)

If you have access to the old repo for inspiration (download/trim/UI only):

| Old piece | Role | Port? |
|-----------|------|-------|
| `services/filmora_14_wfp.py` (~3k LOC) | Clone/patch `.wfp` | **No** — replace |
| `services/wfp_generator.py` | Facade | Replace with `resolve_export` |
| `services/filmora_launcher.py` | Open Filmora | Replace with Resolve launcher |
| `assets/filmora_templates/sermon-highlights.wfp` | Layout template | Replace with Resolve template |
| `services/downloader.py`, `multi_clip.py`, `trimmer.py`, UI | Media prep | **Yes — rewrite/adapt** |
| `utils/windows_paths.py` WSL UNC | Filmora on Windows host | Optional later; not v1 Mac focus |

Canvas constants previously:

```text
DEFAULT_WIDTH = 1080
DEFAULT_HEIGHT = 1920
DEFAULT_FPS = 30.0
DEFAULT_SAMPLE_RATE = 48000
DEFAULT_AUDIO_CHANNELS = 2
```

---

## 17. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| Resolve Free won’t auto-build timelines from Wordly via API | Use file interchange + template; user Import/Open |
| Titles lost in FCPXML | `.srt` sidecar + template instructions |
| Template format changes across Resolve versions | Pin tested Resolve Free version in README; version field in manifest |
| Multi-clip vs single joined slot | Support N clips placed end-to-end on timeline (preferred) or single concat; document choice |
| Large binary templates in git | Prefer XML/OTIO + small stubs; LFS only if needed |

---

## 18. Implementation status

1. Media wizard (download → trim → verse/music) reused in this repo.
2. Filmora `.wfp` stack removed.
3. v1 interchange: **FCPXML + verse.srt** via `services/resolve_export.py`.
4. Do not reintroduce Filmora or Studio-only scripting for the happy path.

---

## 19. Out of scope for v1 (backlog)

- DaVinci Resolve Studio scripting (create project + render unattended).
- Windows / WSL parity.
- AI verse/music suggestions UI.
- CapCut / Premiere targets.
- Full FFmpeg burn-in as primary deliverable (optional secondary).
- In-app visual theme editor.

---

*End of SPEC. When in doubt: preserve the Filmora Wordly user journey; change only the NLE export target to Resolve Free, legally and without watermarks.*
