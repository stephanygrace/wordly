# Resolve Free template authoring

Wordly exports a **1080×1920 @ 30 fps** FCPXML timeline plus `media/` and `verse.srt`. Layout look (stacked video, Text+ styling) should be designed once in Resolve Free; interchange carries clip order, trims, and audio.

## One-time setup

1. Install [DaVinci Resolve Free](https://www.blackmagicdesign.com/products/davinciresolve/).
2. New project → **1080 × 1920**, **30 fps**, **48 kHz** stereo.
3. Import placeholder files named:
   - `Clip001.mp4`, `Clip002.mp4`, …
   - `instrumental.mp3` (or `.m4a` / `.wav`)
   - optional `cover.jpg`
4. Build the creative look (tracks, opacity, titles).
5. Prefer Wordly’s generated FCPXML for media placement; style titles in Resolve after import, or import `verse.srt`.
6. Keep `assets/resolve_templates/sermon-highlights.fcpxml` in the repo (or set `WORDLY_RESOLVE_TEMPLATE`).

## Override

```bash
export WORDLY_RESOLVE_TEMPLATE=/absolute/path/to/your.fcpxml
```

## Import checklist (every sermon)

1. Generate from Wordly → opens Finder on `exports/<name>/`.
2. Resolve → **File → Import → Timeline…** → select `<name>.fcpxml`.
3. Import `verse.srt` if titles did not come across.
4. **Deliver** H.264 1080×1920 — avoid Studio-only effects on Free.
