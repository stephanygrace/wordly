"""Build a DaVinci Resolve Free import bundle (FCPXML + media + verse/caption .srt)."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote
from xml.etree.ElementTree import Element, SubElement, indent

from models.project import ProjectState
from services.caption_style import CaptionStyle, get_caption_style, hex_to_fcpxml
from services.resolve_canvas import (
    CAPTION_LINE_GAP_PER_POINT,
    CAPTION_TITLE_UID,
    DEFAULT_AUDIO_CHANNELS,
    DEFAULT_FPS,
    DEFAULT_HEIGHT,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_WIDTH,
    FCPXML_FRAME_DURATION,
    FCPXML_VERSION,
)
from services.resolve_template import template_available, template_path
from services.transcribe import Caption, transcribe_clips, word_timings
from services import speaker_track
from services.reel_layout import (
    DEFAULT_LAYOUT,
    VerseLine,
    render_background,
    render_main,
    stabilize_if_shaky,
    verse_lines,
)
from services.trimmer import ffprobe_duration_seconds
from utils.export_name import default_export_project_name
from utils.paths import EXPORTS

ProgressCb = Optional[Callable[[float, str], None]]


def _progress(cb: ProgressCb, ratio: float, message: str) -> None:
    if cb is not None:
        cb(ratio, message)


def _export_stem(project_name: str) -> str:
    stem = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in project_name.strip())
    return stem or default_export_project_name()


def _safe_duration(path: Path, fallback_s: float = 60.0) -> float:
    try:
        return max(0.0, ffprobe_duration_seconds(path))
    except Exception:
        return fallback_s


def _copy_media_file(src: Path, dest_dir: Path, dest_name: str) -> Path:
    if not src.is_file():
        raise FileNotFoundError(f"Media file not found: {src}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = (dest_dir / dest_name).resolve()
    src_resolved = src.resolve()
    if dest.is_file() and dest.stat().st_size == src_resolved.stat().st_size:
        return dest
    if src_resolved != dest:
        try:
            if dest.exists():
                dest.unlink()
            os.link(src_resolved, dest)
        except OSError:
            shutil.copy2(src_resolved, dest)
    if dest.stat().st_size <= 0:
        raise ValueError(f"Media file is empty: {dest}")
    return dest


def _file_url(path: Path) -> str:
    resolved = path.resolve()
    # Absolute POSIX path → file URL (macOS / Linux primary target).
    return "file://" + quote(resolved.as_posix(), safe="/:")


def _seconds_to_fcpxml(seconds: float, fps: float = DEFAULT_FPS) -> str:
    """Encode duration/offset as FCPXML rational seconds aligned to frame boundaries."""
    if seconds <= 0:
        return "0s"
    frames = max(1, int(round(seconds * fps)))
    # At 30 fps with frameDuration 100/3000s, one frame = 100/3000s.
    # time = frames * (100/3000) = (frames * 100) / 3000
    numerator = frames * 100
    denominator = 3000
    return f"{numerator}/{denominator}s"


def _srt_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds - math.floor(seconds)) * 1000))
    if millis >= 1000:
        millis = 0
        secs += 1
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _write_verse_srt(path: Path, reference: str, body: str, duration_s: float) -> None:
    text = f"{reference.strip()}\n{body.strip()}".strip()
    end = max(duration_s, 1.0)
    path.write_text(
        f"1\n{_srt_timestamp(0.0)} --> {_srt_timestamp(end)}\n{text}\n",
        encoding="utf-8",
    )


def _write_captions_srt(
    path: Path,
    clip_captions: list[list[Caption]],
    durations_s: list[float],
    style: CaptionStyle | None = None,
) -> int:
    """Write all captions on timeline time (each clip shifted by its offset). Returns count."""
    entries: list[str] = []
    offset_s = 0.0
    for captions, dur_s in zip(clip_captions, durations_s):
        for cap in captions:
            n = len(entries) + 1
            start = _srt_timestamp(offset_s + cap.start)
            end = _srt_timestamp(offset_s + cap.end)
            text = style.display_text(cap.text) if style else cap.text
            entries.append(f"{n}\n{start} --> {end}\n{text}\n")
        offset_s += dur_s
    path.write_text("\n".join(entries), encoding="utf-8")
    return len(entries)


def _frame(seconds: float, fps: float = DEFAULT_FPS) -> int:
    return max(0, int(round(seconds * fps)))


def _frames_to_fcpxml(frames: int) -> str:
    # Same 100/3000s-per-frame encoding as _seconds_to_fcpxml (30 fps).
    return "0s" if frames <= 0 else f"{frames * 100}/3000s"


def _line_break_index(tokens: list[str], max_line_chars: int) -> int | None:
    """Index of the token to end line 1 after, or None when the caption fits one line.

    Picks the split that makes the two lines as even as possible.
    """
    if len(tokens) < 2 or len(" ".join(tokens)) <= max_line_chars:
        return None
    best, best_width = None, None
    for i in range(len(tokens) - 1):
        width = max(len(" ".join(tokens[: i + 1])), len(" ".join(tokens[i + 1 :])))
        if best_width is None or width < best_width:
            best, best_width = i, width
    return best


def _caption_segments(cap: Caption, style: CaptionStyle) -> list[tuple[float, float, list[tuple[str, bool]]]]:
    """(start, end, [(token, highlighted)]) titles for one caption.

    Without a highlight colour that is one title for the whole caption; with
    one, a title per spoken word showing the full caption with that word lit.
    """
    tokens = [(style.display_text(t), s, e) for t, s, e in word_timings(cap)]
    if not tokens:
        return []
    if not style.highlight_color:
        return [(cap.start, cap.end, [(t, False) for t, _, _ in tokens])]
    return [
        (start, end, [(t, j == i) for j, (t, _, _) in enumerate(tokens)])
        for i, (_, start, end) in enumerate(tokens)
    ]


def _add_caption_titles(
    clip_el: Element,
    captions: list[Caption],
    *,
    effect_id: str,
    style: CaptionStyle,
    counter: list[int],
) -> None:
    """Connect Basic Titles for *captions* to *clip_el* (lane 1, plus lane 2 for a second line).

    The parent asset-clip uses start="0s", so a connected clip's offset is the
    caption time relative to the clip's first frame — no timeline shift needed.
    Times are snapped to whole frames; consecutive word titles share boundaries,
    so the highlight moves without gaps or overlaps.

    Resolve drops line breaks inside an imported title, so a caption longer than
    ``style.max_line_chars`` becomes two stacked titles, one per line.
    """
    base = {
        "font": style.font,
        "fontSize": str(style.font_size),
        "fontFace": style.font_face,
        "bold": "1" if "bold" in style.font_face.lower() else "0",
        "alignment": "center",
    }
    if style.outline_width > 0:
        base["strokeColor"] = hex_to_fcpxml(style.outline_color)
        base["strokeWidth"] = str(style.outline_width)
    if style.shadow_color:
        base["shadowColor"] = hex_to_fcpxml(style.shadow_color)
        base["shadowOffset"] = f"{style.shadow_offset} 315"
        base["shadowBlurRadius"] = str(style.shadow_blur)
    colors = {False: hex_to_fcpxml(style.text_color)}
    if style.highlight_color:
        colors[True] = hex_to_fcpxml(style.highlight_color)
    # Measured in Resolve: one line of text is about 0.1% of frame height per font size point.
    line_gap = style.font_size * CAPTION_LINE_GAP_PER_POINT

    def emit(first: int, last: int, lane: int, y: float, runs: list[tuple[str, bool]]) -> None:
        counter[0] += 1
        n = counter[0]
        title = SubElement(
            clip_el,
            "title",
            ref=effect_id,
            lane=str(lane),
            offset=_frames_to_fcpxml(first),
            name=f"Caption {n:03d}",
            duration=_frames_to_fcpxml(last - first),
            start="0s",
        )
        # Resolve ignores the Basic Title "Position" param but honours a transform.
        SubElement(title, "adjust-transform", position=f"0 {y:g}")
        text = SubElement(title, "text")
        # Merge neighbouring tokens of the same colour into one text run.
        merged: list[tuple[str, bool]] = []
        for i, (token, lit) in enumerate(runs):
            piece = token + (" " if i + 1 < len(runs) else "")
            if merged and merged[-1][1] == lit:
                merged[-1] = (merged[-1][0] + piece, lit)
            else:
                merged.append((piece, lit))
        for piece, lit in merged:
            SubElement(text, "text-style", ref=f"ts{n}{'h' if lit else 'n'}").text = piece
        for lit in sorted({lit for _, lit in merged}):
            style_def = SubElement(title, "text-style-def", id=f"ts{n}{'h' if lit else 'n'}")
            SubElement(style_def, "text-style", fontColor=colors[lit], **base)

    for cap in captions:
        for start_s, end_s, runs in _caption_segments(cap, style):
            first, last = _frame(start_s), _frame(end_s)
            if last <= first:
                continue
            break_after = _line_break_index([tok for tok, _ in runs], style.max_line_chars)
            if break_after is None:
                emit(first, last, 1, style.position_y, runs)
            else:
                emit(first, last, 1, style.position_y + line_gap / 2, runs[: break_after + 1])
                emit(first, last, 2, style.position_y - line_gap / 2, runs[break_after + 1 :])


# Background song level under the sermon audio.
MUSIC_GAIN_DB = -15.0

# Captions use lanes 1-2; the verse sits above them.
VERSE_LANE = 3


def _add_verse_titles(
    clip_el: Element,
    lines: list[VerseLine],
    *,
    effect_id: str,
    duration_s: float,
) -> None:
    """The whole verse block as one editable Basic Title for the whole reel.

    Lines are joined with line breaks inside a single text run (Resolve keeps
    breaks there; it drops one placed at the end of a run before another run).
    The title is centred on the middle of the wrapped block.
    """
    if not lines:
        return
    layout = DEFAULT_LAYOUT
    title = SubElement(
        clip_el,
        "title",
        ref=effect_id,
        lane=str(VERSE_LANE),
        offset="0s",
        name="Bible verse",
        duration=_seconds_to_fcpxml(duration_s),
        start="0s",
    )
    centre = (lines[0].position_y + lines[-1].position_y) / 2
    SubElement(title, "adjust-transform", position=f"0 {round(centre, 2):g}")
    SubElement(SubElement(title, "text"), "text-style", ref="tverse").text = "\n".join(line.text for line in lines)
    SubElement(
        SubElement(title, "text-style-def", id="tverse"),
        "text-style",
        font=layout.verse_font,
        fontSize=str(layout.verse_resolve_size),
        fontFace="SemiBold",
        fontColor=hex_to_fcpxml(layout.verse_color),
        bold="1",
        shadowColor="0 0 0 0.6",
        shadowOffset="3 315",
        shadowBlurRadius="2",
        alignment="center",
    )


@dataclass(frozen=True)
class ResolveExportBundle:
    bundle_dir: Path
    timeline_path: Path
    media_dir: Path
    clip_paths: list[Path]
    music: Path | None
    cover: Path | None
    verse_txt: Path | None
    verse_srt: Path | None
    captions_srt: Path | None
    manifest_path: Path


def _collect_clip_sources(project: ProjectState) -> list[Path]:
    clips = [p.resolve() for p in project.clip_paths if p.exists()]
    if clips:
        return clips
    if project.joined_clip_path and project.joined_clip_path.exists():
        return [project.joined_clip_path.resolve()]
    raise ValueError(
        "At least one highlight clip (Clip001.mp4…) is required before generating "
        "a Resolve project."
    )


def _prepare_media(
    project: ProjectState,
    *,
    bundle_dir: Path,
    progress_cb: ProgressCb = None,
) -> tuple[list[Path], Path | None, Path | None, Path | None]:
    media_dir = bundle_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    sources = _collect_clip_sources(project)
    _progress(progress_cb, 0.15, "Copying highlight clips…")
    clip_paths: list[Path] = []
    for idx, src in enumerate(sources, start=1):
        ext = src.suffix.lower() or ".mp4"
        dest_name = f"Clip{idx:03d}{ext}"
        clip_paths.append(_copy_media_file(src, media_dir, dest_name))

    music: Path | None = None
    if project.selected_music and project.selected_music.local_path:
        music_src = project.selected_music.local_path.resolve()
        if music_src.is_file():
            _progress(progress_cb, 0.35, "Copying instrumental…")
            ext = music_src.suffix.lower() or ".mp3"
            music = _copy_media_file(music_src, media_dir, f"instrumental{ext}")

    cover: Path | None = None
    sermon = project.sermon_path.resolve() if project.sermon_path and project.sermon_path.is_file() else None
    if sermon is not None:
        _progress(progress_cb, 0.45, "Extracting cover still…")
        cover_candidate = media_dir / "cover.jpg"
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-ss",
                    "1",
                    "-i",
                    str(sermon),
                    "-frames:v",
                    "1",
                    "-q:v",
                    "2",
                    str(cover_candidate),
                ],
                check=True,
                capture_output=True,
            )
            if cover_candidate.is_file() and cover_candidate.stat().st_size > 0:
                cover = cover_candidate.resolve()
        except Exception:
            cover = None

    verse_txt: Path | None = None
    if project.selected_verse:
        verse_txt = bundle_dir / "verse.txt"
        verse_txt.write_text(
            f"{project.selected_verse.reference}\n\n{project.selected_verse.text}\n",
            encoding="utf-8",
        )

    return clip_paths, music, cover, verse_txt


def _prepare_reel_media(
    project: ProjectState,
    clip_paths: list[Path],
    media_dir: Path,
    progress_cb: ProgressCb = None,
) -> tuple[list[Path], list[Path], list[VerseLine]]:
    """Per clip: background (head to feet) and main video (head to waist); plus the verse lines.

    Main videos are only made when the speaker can be tracked; otherwise the
    timeline keeps the original clips.
    """
    backgrounds: list[Path] = []
    mains: list[Path] = []
    tracking = speaker_track.vision_available()
    for idx, clip in enumerate(clip_paths, start=1):
        _progress(
            progress_cb,
            0.70 + 0.17 * (idx - 1) / max(1, len(clip_paths)),
            f"Following the speaker in clip {idx}/{len(clip_paths)}…",
        )
        # Handheld footage is stabilised first; tripod footage is used as is.
        source = stabilize_if_shaky(clip, media_dir / f"{clip.stem}_stab.mp4")
        # Reels have one speaker: follow the most prominent person in each clip.
        camera = speaker_track.analyze(source) if tracking else None
        backgrounds.append(render_background(source, media_dir / f"{clip.stem}_bg.mp4", camera=camera))
        if camera is not None:
            mains.append(render_main(source, media_dir / f"{clip.stem}_main.mp4", camera))
    verse: list[VerseLine] = []
    if project.selected_verse:
        _progress(progress_cb, 0.87, "Laying out verse text…")
        verse = verse_lines(project.selected_verse.reference, project.selected_verse.text)
    return backgrounds, mains, verse


def _build_fcpxml(
    *,
    project_name: str,
    clip_paths: list[Path],
    music: Path | None,
    timeline_duration_s: float,
    clip_captions: list[list[Caption]] | None = None,
    caption_style: CaptionStyle | None = None,
    backgrounds: list[Path] | None = None,
    verse: list[VerseLine] | None = None,
) -> Element:
    root = Element("fcpxml", version=FCPXML_VERSION)
    resources = SubElement(root, "resources")
    format_id = "r1"
    SubElement(
        resources,
        "format",
        id=format_id,
        name="FFVideoFormat1080p30",
        frameDuration=FCPXML_FRAME_DURATION,
        width=str(DEFAULT_WIDTH),
        height=str(DEFAULT_HEIGHT),
        colorSpace="1-1-1 (Rec. 709)",
    )

    asset_ids: list[str] = []
    durations_s: list[float] = []
    next_id = 2

    for clip in clip_paths:
        asset_id = f"r{next_id}"
        next_id += 1
        dur_s = _safe_duration(clip)
        durations_s.append(dur_s)
        dur = _seconds_to_fcpxml(dur_s)
        src = _file_url(clip)
        asset = SubElement(
            resources,
            "asset",
            id=asset_id,
            name=clip.stem,
            start="0s",
            duration=dur,
            hasVideo="1",
            hasAudio="1",
            format=format_id,
            videoSources="1",
            audioSources="1",
            audioChannels=str(DEFAULT_AUDIO_CHANNELS),
            audioRate=str(DEFAULT_SAMPLE_RATE),
        )
        asset.set("src", src)
        SubElement(asset, "media-rep", kind="original-media", src=src)
        asset_ids.append(asset_id)

    background_ids: list[str] = []
    for bg in backgrounds or []:
        bg_id = f"r{next_id}"
        next_id += 1
        src = _file_url(bg)
        asset = SubElement(
            resources,
            "asset",
            id=bg_id,
            name=bg.stem,
            start="0s",
            duration=_seconds_to_fcpxml(_safe_duration(bg)),
            hasVideo="1",
            hasAudio="0",
            format=format_id,
            videoSources="1",
        )
        asset.set("src", src)
        SubElement(asset, "media-rep", kind="original-media", src=src)
        background_ids.append(bg_id)

    music_id: str | None = None
    if music is not None:
        music_id = f"r{next_id}"
        next_id += 1
        music_dur_s = _safe_duration(music, fallback_s=timeline_duration_s)
        music_dur = _seconds_to_fcpxml(max(music_dur_s, timeline_duration_s))
        src = _file_url(music)
        asset = SubElement(
            resources,
            "asset",
            id=music_id,
            name=music.stem,
            start="0s",
            duration=music_dur,
            hasVideo="0",
            hasAudio="1",
            audioSources="1",
            audioChannels=str(DEFAULT_AUDIO_CHANNELS),
            audioRate=str(DEFAULT_SAMPLE_RATE),
        )
        asset.set("src", src)
        SubElement(asset, "media-rep", kind="original-media", src=src)

    caption_effect_id: str | None = None
    if (clip_captions and any(clip_captions)) or verse:
        caption_effect_id = f"r{next_id}"
        next_id += 1
        SubElement(resources, "effect", id=caption_effect_id, name="Basic Title", uid=CAPTION_TITLE_UID)
    caption_counter = [0]
    style = caption_style or get_caption_style(None)

    library = SubElement(root, "library")
    event = SubElement(library, "event", name="Wordly")
    proj = SubElement(event, "project", name=project_name)
    sequence = SubElement(
        proj,
        "sequence",
        format=format_id,
        duration=_seconds_to_fcpxml(timeline_duration_s),
        tcStart="0s",
        tcFormat="NDF",
        audioLayout="stereo",
        audioRate="48k",
    )
    spine = SubElement(sequence, "spine")

    offset_s = 0.0
    for idx, (asset_id, dur_s) in enumerate(zip(asset_ids, durations_s)):
        clip_el = SubElement(
            spine,
            "asset-clip",
            ref=asset_id,
            offset=_seconds_to_fcpxml(offset_s),
            name=f"Clip{idx + 1:03d}",
            duration=_seconds_to_fcpxml(dur_s),
            start="0s",
            tcFormat="NDF",
        )
        if idx < len(background_ids):
            # Under the sermon video; Resolve fits the 16:9 clip full width above it.
            SubElement(
                clip_el,
                "asset-clip",
                ref=background_ids[idx],
                lane="-1",
                offset="0s",
                name=f"Clip{idx + 1:03d} background",
                duration=_seconds_to_fcpxml(dur_s),
                start="0s",
                tcFormat="NDF",
            )
        if idx == 0 and verse and caption_effect_id is not None:
            _add_verse_titles(
                clip_el, verse, effect_id=caption_effect_id, duration_s=timeline_duration_s
            )
        if idx == 0 and music_id is not None:
            music_el = SubElement(
                clip_el,
                "asset-clip",
                ref=music_id,
                lane="-2" if background_ids else "-1",
                offset="0s",
                name="Instrumental",
                duration=_seconds_to_fcpxml(timeline_duration_s),
                start="0s",
                tcFormat="NDF",
            )
            # Clip volume (editable in Resolve's Inspector), not baked into the file.
            SubElement(music_el, "adjust-volume", amount=f"{MUSIC_GAIN_DB:g}dB")
        if caption_effect_id is not None and clip_captions and idx < len(clip_captions):
            _add_caption_titles(
                clip_el,
                clip_captions[idx],
                effect_id=caption_effect_id,
                style=style,
                counter=caption_counter,
            )
        offset_s += dur_s

    return root


def _write_fcpxml(path: Path, root: Element) -> None:
    from xml.etree.ElementTree import tostring

    indent(root, space="  ")
    body = tostring(root, encoding="utf-8", xml_declaration=False)
    path.write_bytes(
        b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n' + body
    )


def generate_resolve_project(
    project: ProjectState,
    *,
    output_dir: Path | None = None,
    progress_cb: ProgressCb = None,
) -> Path:
    """
    Write exports/<name>/ with FCPXML timeline, media/, verse/caption sidecars, and README.

    When ``project.auto_captions`` is set, each clip is transcribed with Whisper
    and the captions are added as styled titles plus a ``captions.srt`` fallback.

    Returns the primary timeline path (.fcpxml).
    """
    if not template_available():
        raise FileNotFoundError(
            "Resolve template not found. Add assets/resolve_templates/sermon-highlights.fcpxml "
            "or set WORDLY_RESOLVE_TEMPLATE to an FCPXML/OTIO file. "
            "See docs/TEMPLATE_AUTHORING.md."
        )

    # Template presence gates export; content is generated for Free-compatible import.
    _ = template_path()

    if not project.selected_verse:
        raise ValueError("Bible verse is required before generating a Resolve project.")
    music = project.selected_music
    if not music or not music.local_path or not music.local_path.is_file():
        raise ValueError("Local instrumental audio is required before generating a Resolve project.")

    stem = _export_stem(project.project_name or default_export_project_name())
    bundle_dir = (output_dir or (EXPORTS / stem)).resolve()
    bundle_dir.mkdir(parents=True, exist_ok=True)

    _progress(progress_cb, 0.05, "Preparing Resolve export bundle…")
    clip_paths, music_path, cover, verse_txt = _prepare_media(
        project, bundle_dir=bundle_dir, progress_cb=progress_cb
    )

    durations = [_safe_duration(p) for p in clip_paths]
    timeline_duration_s = sum(durations) if durations else 1.0

    clip_captions: list[list[Caption]] | None = None
    captions_srt: Path | None = None
    caption_count = 0
    caption_style = get_caption_style(project.caption_style)
    if project.auto_captions:
        if project.captions_current() and len(project.clip_captions) == len(clip_paths):
            # Reviewed/edited in the wizard's Captions step.
            clip_captions = project.clip_captions
            _progress(progress_cb, 0.7, "Using reviewed captions…")
        else:
            clip_captions = transcribe_clips(
                clip_paths,
                durations,
                progress_cb=progress_cb,
                ratio_range=(0.5, 0.7),
            )
        captions_srt = bundle_dir / "captions.srt"
        caption_count = _write_captions_srt(captions_srt, clip_captions, durations, caption_style)

    backgrounds: list[Path] = []
    verse: list[VerseLine] = []
    timeline_clips = clip_paths
    if project.reel_layout:
        backgrounds, mains, verse = _prepare_reel_media(
            project, clip_paths, bundle_dir / "media", progress_cb
        )
        if len(mains) == len(clip_paths):
            # Zoomed head-to-waist versions (same length and audio) replace the originals.
            timeline_clips = mains

    _progress(progress_cb, 0.89, "Writing verse subtitle…")
    verse_srt = bundle_dir / "verse.srt"
    _write_verse_srt(
        verse_srt,
        project.selected_verse.reference,
        project.selected_verse.text,
        timeline_duration_s,
    )

    _progress(progress_cb, 0.92, "Writing FCPXML timeline…")
    root = _build_fcpxml(
        project_name=project.project_name or stem,
        clip_paths=timeline_clips,
        music=music_path,
        timeline_duration_s=timeline_duration_s,
        clip_captions=clip_captions,
        caption_style=caption_style,
        backgrounds=backgrounds,
        verse=verse,
    )
    timeline_path = bundle_dir / f"{stem}.fcpxml"
    _write_fcpxml(timeline_path, root)

    # Keep a copy of the bundled template marker for operators who open the folder.
    tmpl = template_path()
    if tmpl is not None and tmpl.suffix.lower() == ".fcpxml":
        shutil.copy2(tmpl, bundle_dir / f"_template_{tmpl.name}")

    manifest = {
        "wordly_export": True,
        "nle": "davinci_resolve_free",
        "interchange": "fcpxml",
        "canvas": {
            "width": DEFAULT_WIDTH,
            "height": DEFAULT_HEIGHT,
            "fps": DEFAULT_FPS,
            "sample_rate": DEFAULT_SAMPLE_RATE,
            "audio_channels": DEFAULT_AUDIO_CHANNELS,
        },
        "project_name": project.project_name,
        "timeline": str(timeline_path),
        "media": {
            "clips": [str(p) for p in clip_paths],
            "music": str(music_path) if music_path else None,
            "cover": str(cover) if cover else None,
            "verse_txt": str(verse_txt) if verse_txt else None,
            "verse_srt": str(verse_srt),
            "captions_srt": str(captions_srt) if captions_srt else None,
        },
        "captions": caption_count,
        "caption_style": caption_style.name if clip_captions else None,
        "reel_layout": project.reel_layout,
        "fb_url": project.fb_url,
        "segments": [
            {"start": s.start_text, "end": s.end_text, "label": s.label}
            for s in project.segments
        ],
        "resolve_template": str(tmpl) if tmpl else None,
    }
    manifest_path = bundle_dir / "wordly_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    readme = bundle_dir / "OPEN_IN_RESOLVE.txt"
    readme.write_text(
        "Open this Wordly export in DaVinci Resolve Free\n"
        "================================================\n\n"
        f"1. Reveal this folder in Finder:\n   {bundle_dir}\n\n"
        f"2. In Resolve: File → Import → Timeline…\n"
        f"   Select:\n   {timeline_path.name}\n\n"
        "3. Confirm media is online (files live in media/).\n\n"
        "4. Import verse.srt (File → Import → Subtitle…) for the on-screen verse,\n"
        "   or copy text from verse.txt into a Text+ title in your template.\n\n"
        + (
            "5. Auto captions are on video track 2 as titles. If they import unstyled,\n"
            "   delete that track and import captions.srt (File → Import → Subtitle…)\n"
            "   instead, then style the subtitle track in the Inspector.\n\n"
            if captions_srt
            else ""
        )
        + f"{6 if captions_srt else 5}. Deliver → export H.264 / 1080×1920.\n\n"
        "Resolve Free does not watermark normal exports. Avoid Studio-only effects\n"
        "in the timeline (those can watermark Free renders).\n"
        "Keep this folder together — the .fcpxml and media/ must stay side by side.\n",
        encoding="utf-8",
    )

    project.resolve_output_path = timeline_path
    _progress(progress_cb, 1.0, "Resolve project ready")
    return timeline_path


# Back-compat alias used by thinner facades / tests.
generate_resolve = generate_resolve_project
