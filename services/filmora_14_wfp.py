from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import subprocess
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from models.project import ProjectState
from services.filmora_14 import FILMORA_BUILD
from services.filmora_template import TEMPLATE_DIR, template_media_bundle, template_path
from services.trimmer import ffprobe_duration_seconds, parse_trim_times
from utils.console_log import log_error, log_info, log_step, log_warn
from utils.paths import EXPORTS, TEMP
from utils.windows_paths import filmora_media_path

# Legacy IDs (old bundled template) — kept for unit tests.
TEMPLATE_VIDEO_ID = "{84A7362C-C821-4503-827F-B261E040D2BF}"
TEMPLATE_SOURCE_VIDEO_ID = "{B58CC5D9-D4EA-4091-9FA5-863110B420D0}"
TEMPLATE_AUDIO_ID = "{C19B628D-ACB4-42a5-8DF4-3ADC315A6539}"
TEMPLATE_TIMELINE_ID = "{CE578FD0-98CF-4080-A85C-D05F1DCA0A93}"

TEMPLATE_TIMELINE_HIGHLIGHT_FILE = "Facebook.mp4"
TEMPLATE_TIMELINE_SOURCE_FILE = "Facebook_1.mp4"

_STATIC_PATH_MARKERS = (
    TEMPLATE_TIMELINE_SOURCE_FILE,
    TEMPLATE_TIMELINE_HIGHLIGHT_FILE,
    "Any Video Converter",
    "Format Convert",
    "placeholder_highlight",
    "placeholder_source",
    "placeholder_music",
    "placeholder_thumb",
)

REFERENCE_MEDIA_DIR_NAME = "reference_media"

MEDIA_TYPE_VIDEO = 8
MEDIA_TYPE_AUDIO = 4
MEDIA_TYPE_IMAGE = 16


@dataclass(frozen=True)
class TemplateLayout:
    """Media GUID layout from a real Filmora 14 .wfp template."""

    timeline_id: str
    video_ids: tuple[str, ...]
    audio_ids: tuple[str, ...]
    image_ids: tuple[str, ...]
    path_markers: tuple[str, ...]
    source_video_id: str = ""
    joined_video_id: str = ""
    source_clip_uuid: str = ""
    joined_clip_uuid: str = ""

    @property
    def timeline_rel_path(self) -> str:
        return f"ProjectFolder/Medias/{self.timeline_id}/timeline.wesproj"

    @property
    def primary_video_id(self) -> str:
        return self.source_video_id or (self.video_ids[0] if self.video_ids else "")

    @property
    def secondary_video_id(self) -> str:
        return self.joined_video_id or (
            self.video_ids[1] if len(self.video_ids) > 1 else self.primary_video_id
        )


def _load_template_layout(
    project_info: dict[str, Any],
    medias_info: dict[str, Any],
    timeline_data: dict[str, Any] | None = None,
) -> TemplateLayout:
    timeline_id = str(project_info.get("timeline_mediaId", ""))
    if not timeline_id:
        raise ValueError(
            "Filmora template is missing timeline_mediaId. Save a working Filmora 14 "
            "project as assets/filmora_templates/filmora_14_2_9.wfp."
        )

    items: dict[str, Any] = medias_info.get("media_items", {})
    videos: list[str] = []
    audios: list[str] = []
    images: list[str] = []
    markers: list[str] = list(_STATIC_PATH_MARKERS)

    for media_id, item in items.items():
        media_type = int(item.get("media_type", 0))
        if media_type == MEDIA_TYPE_VIDEO:
            videos.append(media_id)
        elif media_type == MEDIA_TYPE_AUDIO:
            audios.append(media_id)
        elif media_type == MEDIA_TYPE_IMAGE:
            images.append(media_id)

        for key in ("download_url", "file_name", "name"):
            raw = str(item.get(key, "") or "")
            if not raw or raw.startswith("{"):
                continue
            name = Path(raw.replace("/", "\\")).name
            if name and name not in markers:
                markers.append(name)

    if timeline_data is not None:
        for res in timeline_data.get("resources", []):
            fn = str(res.get("filename", "") or "")
            name = Path(fn.replace("/", "\\").split("file:")[-1]).name
            if name and name not in markers:
                markers.append(name)

    if not videos:
        if timeline_data is None:
            raise ValueError("Filmora template has no video media entries.")
        source_clip_uuid, joined_clip_uuid = _detect_clip_uuids(timeline_data)
        return TemplateLayout(
            timeline_id=timeline_id,
            video_ids=(),
            audio_ids=tuple(audios),
            image_ids=tuple(images),
            path_markers=tuple(markers),
            source_video_id="",
            joined_video_id="",
            source_clip_uuid=source_clip_uuid,
            joined_clip_uuid=joined_clip_uuid,
        )

    source_video_id, joined_video_id = _infer_template_video_roles(items)

    return TemplateLayout(
        timeline_id=timeline_id,
        video_ids=tuple(videos),
        audio_ids=tuple(audios),
        image_ids=tuple(images),
        path_markers=tuple(markers),
        source_video_id=source_video_id,
        joined_video_id=joined_video_id,
    )


def _infer_template_video_roles(items: dict[str, Any]) -> tuple[str, str]:
    """Map template media GUIDs to sermon source vs joined highlight reel (GSM layout)."""
    source_id = ""
    joined_id = ""
    video_ids: list[str] = []

    for media_id, item in items.items():
        if int(item.get("media_type", 0)) != MEDIA_TYPE_VIDEO:
            continue
        video_ids.append(media_id)
        text = " ".join(
            str(item.get(key, "") or "") for key in ("download_url", "file_name", "name")
        ).lower()
        if "copy_" in text or TEMPLATE_TIMELINE_HIGHLIGHT_FILE.lower() in text:
            joined_id = media_id
        elif any(
            marker in text
            for marker in (
                TEMPLATE_TIMELINE_SOURCE_FILE.lower(),
                "facebook_1",
                "placeholder_source",
                "sunday service",
                "format convert",
            )
        ):
            source_id = media_id
        elif "facebook.mp4" in text and "facebook_1" not in text:
            joined_id = media_id or joined_id

    if not source_id and video_ids:
        source_id = video_ids[0]
    if not joined_id:
        joined_id = video_ids[1] if len(video_ids) > 1 else video_ids[0]
    return source_id, joined_id


def _detect_clip_uuids(timeline_data: dict[str, Any]) -> tuple[str, str]:
    """Read timeline clip sourceUuid values tied to source vs joined media."""
    source_uuid = ""
    joined_uuid = ""
    for timeline_info in timeline_data.get("timelineInfos", []):
        for track in timeline_info.get("trackInfos", []):
            for clip in track.get("clipList") or []:
                filename = str(clip.get("filename", "") or "").lower()
                clip_uuid = str(clip.get("sourceUuid", "") or "")
                if not clip_uuid:
                    continue
                if "copy_" in filename or TEMPLATE_TIMELINE_HIGHLIGHT_FILE.lower() in filename:
                    joined_uuid = clip_uuid
                elif _VIDEO_FILE_RE.search(filename) and not _IMAGE_FILE_RE.search(filename):
                    if "copy_" not in filename:
                        source_uuid = clip_uuid
    return source_uuid, joined_uuid


def _layout_with_timeline_uuids(
    layout: TemplateLayout, timeline_data: dict[str, Any]
) -> TemplateLayout:
    source_clip_uuid, joined_clip_uuid = _detect_clip_uuids(timeline_data)
    if not source_clip_uuid and not joined_clip_uuid:
        return layout
    return TemplateLayout(
        timeline_id=layout.timeline_id,
        video_ids=layout.video_ids,
        audio_ids=layout.audio_ids,
        image_ids=layout.image_ids,
        path_markers=layout.path_markers,
        source_video_id=layout.source_video_id,
        joined_video_id=layout.joined_video_id,
        source_clip_uuid=source_clip_uuid,
        joined_clip_uuid=joined_clip_uuid,
    )


def _sync_clip_paths_to_source_uuid(
    clips: list[dict[str, Any]],
    *,
    layout: TemplateLayout,
    source_url: str,
    joined_url: str,
) -> None:
    """Timeline clips must reference the same file as their sourceUuid media pool entry."""
    if (
        layout.source_clip_uuid
        and layout.joined_clip_uuid
        and layout.source_clip_uuid == layout.joined_clip_uuid
    ):
        # Single-video templates (e.g. sermon-highlights.wfp) — paths set per clip above.
        return
    for clip in clips:
        clip_uuid = str(clip.get("sourceUuid", "") or "")
        if layout.source_clip_uuid and clip_uuid == layout.source_clip_uuid:
            if not _is_cover_clip(clip):
                clip["filename"] = source_url
        elif layout.joined_clip_uuid and clip_uuid == layout.joined_clip_uuid:
            clip["filename"] = joined_url


def _export_stem(project_name: str) -> str:
    stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in project_name.strip())
    return stem or "wordly-project"


@dataclass(frozen=True)
class ExportMediaBundle:
    """Self-contained export folder: .wfp plus copied media with simple paths."""

    bundle_dir: Path
    wfp_path: Path
    media_dir: Path
    joined: Path
    source: Path
    music: Path | None
    cover: Path | None
    verse_path: Path | None
    manifest_path: Path


def _copy_media_file(src: Path, dest_dir: Path, dest_name: str) -> Path:
    if not src.is_file():
        raise FileNotFoundError(f"Media file not found: {src}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = (dest_dir / dest_name).resolve()
    if src.resolve() != dest:
        shutil.copy2(src, dest)
    if dest.stat().st_size <= 0:
        raise ValueError(f"Media file is empty: {dest}")
    return dest


def _prepare_export_bundle(
    project: ProjectState,
    stem: str | None = None,
    *,
    bundle_dir: Path | None = None,
) -> ExportMediaBundle:
    """
    Copy sermon, joined highlights, music, and verse text into exports/<stem>/media/
    so Filmora always receives paths that exist beside the .wfp.
    """
    if not project.joined_clip_path or not project.joined_clip_path.exists():
        raise ValueError("Joined highlight video is required before exporting a Filmora project.")

    safe_stem = stem or _export_stem(project.project_name)
    bundle_dir = (bundle_dir or (EXPORTS / safe_stem)).resolve()
    media_dir = bundle_dir / "media"
    bundle_dir.mkdir(parents=True, exist_ok=True)

    joined_src = project.joined_clip_path.resolve()
    joined = _copy_media_file(
        joined_src,
        media_dir,
        f"highlights_joined{joined_src.suffix.lower() or '.mp4'}",
    )

    sermon = project.sermon_path.resolve() if project.sermon_path and project.sermon_path.is_file() else None
    if sermon and sermon.resolve() != joined_src.resolve():
        source = _copy_media_file(
            sermon,
            media_dir,
            f"source_sermon{sermon.suffix.lower() or '.mp4'}",
        )
    else:
        source = joined

    music: Path | None = None
    if project.selected_music and project.selected_music.local_path:
        music_src = project.selected_music.local_path.resolve()
        if music_src.is_file():
            ext = music_src.suffix.lower() or ".mp3"
            music = _copy_media_file(music_src, media_dir, f"instrumental{ext}")

    cover: Path | None = None
    if sermon and sermon.is_file():
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
            pass

    verse_path: Path | None = None
    if project.selected_verse:
        verse_path = bundle_dir / "verse.txt"
        verse_path.write_text(
            f"{project.selected_verse.reference}\n\n{project.selected_verse.text}\n",
            encoding="utf-8",
        )

    manifest = {
        "wordly_export": True,
        "project_name": project.project_name,
        "wfp": str((bundle_dir / f"{safe_stem}.wfp").resolve()),
        "media": {
            "joined": str(joined),
            "source": str(source),
            "music": str(music) if music else None,
            "cover": str(cover) if cover else None,
            "verse": str(verse_path) if verse_path else None,
        },
        "fb_url": project.fb_url,
        "segments": [
            {"start": s.start_text, "end": s.end_text, "label": s.label}
            for s in project.segments
        ],
    }
    manifest_path = bundle_dir / "wordly_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    readme = bundle_dir / "OPEN_IN_FILMORA.txt"
    readme.write_text(
        f"Open this project in Filmora 14.2.9:\n\n"
        f"  {bundle_dir / f'{safe_stem}.wfp'}\n\n"
        f"Keep this folder together — the .wfp and the media/ subfolder must stay "
        f"in the same place.\n",
        encoding="utf-8",
    )

    return ExportMediaBundle(
        bundle_dir=bundle_dir,
        wfp_path=(bundle_dir / f"{safe_stem}.wfp").resolve(),
        media_dir=media_dir,
        joined=joined,
        source=source,
        music=music,
        cover=cover,
        verse_path=verse_path,
        manifest_path=manifest_path,
    )


def _guid() -> str:
    return "{" + str(uuid.uuid4()).upper() + "}"


def _media_file_md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _us(seconds: float) -> int:
    return int(max(0.0, seconds) * 1_000_000)


def _filmora_media_length_units(duration_us: int) -> int:
    """media.json ``mediaLength`` / ``streamLength`` use 100 ns units (10× microseconds)."""
    return max(0, int(duration_us)) * 10


def _sync_media_json_duration(payload: dict[str, Any], duration_us: int) -> None:
    """Keep media pool metadata aligned with the exported file on disk."""
    if duration_us <= 0:
        return
    units = _filmora_media_length_units(duration_us)
    source_info = payload.get("sourceInfo")
    if not isinstance(source_info, dict):
        return
    basic = source_info.get("basicInfo")
    if isinstance(basic, dict):
        basic["mediaLength"] = units
    for key in ("vidStreamInfos", "audStreamInfos"):
        streams = source_info.get(key)
        if isinstance(streams, list):
            for stream in streams:
                if isinstance(stream, dict):
                    stream["streamLength"] = units


def _validate_export_segments(
    project: ProjectState,
    *,
    source_path: Path,
    joined_path: Path,
    source_duration_us: int,
) -> None:
    """Reject exports that reference sermon timestamps beyond the source file."""
    windows = _segment_windows_us(project)
    if not windows or source_duration_us <= 0:
        return
    max_end_us = max(out_pt for _, out_pt, _ in windows)
    if max_end_us <= source_duration_us + 1_000_000:
        return
    same_file = source_path.resolve() == joined_path.resolve()
    extra = (
        "Wordly is using the joined highlights file as the only video source, but your "
        "segments use timestamps from the full Facebook sermon. Export again after the "
        "full sermon download is available (Download step), or re-enter segment times "
        "relative to the joined clip only."
        if same_file
        else ""
    )
    raise ValueError(
        f"Segment end time ({max_end_us / 1_000_000:.1f}s) is past the sermon file length "
        f"({source_duration_us / 1_000_000:.1f}s).\n\n"
        f"Source: {source_path}"
        + (f"\n\n{extra}" if extra else "")
    )


def _safe_duration(path: Path, fallback_s: float) -> float:
    try:
        return ffprobe_duration_seconds(path)
    except Exception:
        return fallback_s


def _read_json(raw: bytes) -> Any:
    return json.loads(raw.decode("utf-8"))


def _write_json(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _filmora_path_str(path: Path) -> str:
    """Return a Filmora-friendly Windows path string."""
    win = filmora_media_path(path)
    return _json_safe_media_path(win)


def _filmora_file_url(path: Path) -> str:
    """Timeline clip paths use a file:/ prefix."""
    win = _filmora_path_str(path)
    if win.startswith("file:"):
        return win
    return "file:/" + win.lstrip("/")


def _json_safe_media_path(path: str) -> str:
    """Paths embedded in Filmora JSON must not contain raw backslashes."""
    if not path:
        return path
    # WSL UNC paths must keep backslashes — forward slashes break Filmora on Windows.
    if path.startswith("\\\\"):
        return path
    return path.replace("\\", "/")


def _path_variants(path: str) -> set[str]:
    """Generate common spellings Filmora stores for the same file path."""
    if not path:
        return set()
    variants: set[str] = {path}
    fwd = path.replace("\\", "/")
    back = path.replace("/", "\\")
    variants.add(fwd)
    variants.add(back)
    if fwd.startswith("//"):
        variants.add("\\\\" + fwd[2:].replace("/", "\\"))
    if path.startswith("\\\\"):
        unc_fwd = "//" + path[2:].replace("\\", "/")
        variants.add(unc_fwd)
    if "://" not in path and re.match(r"^[A-Za-z]:", path):
        for base in (fwd, back):
            variants.add(f"file:///{base}")
            variants.add(f"file://{base}")
            variants.add(f"file:/{base}")
    return {v for v in variants if v}


def _replacement_target_for_old(old_var: str, new_path: str) -> str:
    """Map a legacy path spelling to a JSON-safe replacement (forward slashes)."""
    safe_new = _json_safe_media_path(new_path)
    if old_var.startswith("file:"):
        if safe_new.startswith("file:"):
            return _json_safe_media_path(safe_new)
        path_part = safe_new[1:] if re.match(r"^/[A-Za-z]:", safe_new) else safe_new
        return _filmora_file_url(Path(path_part))
    return safe_new


def _sanitize_timeline_filenames(text: str) -> str:
    """
    Normalize clip filename URLs in timeline.wesproj.

    Partial path replacements can leave Windows backslashes in JSON (e.g. \\P in
    "Piano"), which breaks parsing and makes Filmora show "can't open file".
    Filmora 14 on Windows expects ``file:/C:/...`` (two slashes), not file:///.
    """

    def repl(match: re.Match[str]) -> str:
        url = match.group(1)
        if url.startswith("file:///\\\\") or url.startswith("file://\\\\"):
            return match.group(0)
        if url.startswith("file:///"):
            body = url[7:].replace("\\", "/").lstrip("/")
            return f'"filename":"file:/{body}"'
        if url.startswith("file://"):
            body = url[6:].replace("\\", "/").lstrip("/")
            return f'"filename":"file:/{body}"'
        if url.startswith("file:/"):
            body = url[5:].replace("\\", "/").lstrip("/")
            return f'"filename":"file:/{body}"'
        return match.group(0)

    return re.sub(r'"filename":"(file:[^"]+)"', repl, text)


def _validate_timeline_json(text: str) -> None:
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "Exported Filmora timeline is invalid JSON (often caused by backslashes in "
            f"media paths). Details: {exc}"
        ) from exc


def _finalize_timeline(
    text: str,
    *,
    project: ProjectState,
    source_duration_us: int,
    joined_duration_us: int,
    audio_duration_us: int,
    timeline_duration_us: int,
    trim_style: str | None = None,
) -> str:
    """Sanitize paths and repair clip trims — required after path substitution."""
    text = _sanitize_timeline_filenames(text)
    if trim_style is None:
        try:
            trim_style = _detect_timeline_trim_style(json.loads(text))
        except json.JSONDecodeError:
            trim_style = None
    text = _repair_timeline_inouts(
        text,
        project=project,
        source_duration_us=source_duration_us,
        joined_duration_us=joined_duration_us,
        audio_duration_us=audio_duration_us,
        timeline_duration_us=timeline_duration_us,
        trim_style=trim_style,
    )
    _validate_timeline_json(text)
    return text


def _register_replacement(replacements: dict[str, str], old: str, new: str) -> None:
    if not old or not new or old == new:
        return
    safe_new = _json_safe_media_path(new)
    replacements[old] = safe_new
    for old_var in _path_variants(old):
        replacements[old_var] = _replacement_target_for_old(old_var, safe_new)
    if re.match(r"^[A-Za-z]:", safe_new):
        replacements[f"file:///{safe_new}"] = _filmora_file_url(Path(safe_new))
        replacements[f"file://{safe_new}"] = _filmora_file_url(Path(safe_new))


def _replace_paths_in_text(text: str, replacements: dict[str, str]) -> str:
    out = text
    # Longest keys first so partial paths do not mask full paths.
    for old in sorted(replacements, key=len, reverse=True):
        new = replacements[old]
        if not old or old == new:
            continue
        out = out.replace(old, new)
    return out


_VIDEO_FILE_RE = re.compile(r"\.(mp4|mov|m4v)(?:\"|$)", re.IGNORECASE)
_AUDIO_FILE_RE = re.compile(r"\.(mp3|m4a|wav|aac|flac)(?:\"|$)", re.IGNORECASE)
_IMAGE_FILE_RE = re.compile(r"\.(jpg|jpeg|png)(?:\"|$)", re.IGNORECASE)
_JOINED_REEL_MARKERS = (
    "copy_",
    "placeholder_highlight",
    TEMPLATE_TIMELINE_HIGHLIGHT_FILE,
)
_SOURCE_SEGMENT_MARKERS = (
    TEMPLATE_TIMELINE_SOURCE_FILE,
    "placeholder_source",
    "source_sermon",
)
CLIP_TYPE_TITLE = 7
CLIP_TYPE_SCRIPT = 4
REFERENCE_VERSE_TIMELINE_INFO_PATH = TEMPLATE_DIR / "reference_verse_timeline_info.json"
REFERENCE_VERSE_SCRIPT_CLIP_PATH = TEMPLATE_DIR / "reference_verse_script_clip.json"
COVER_CLIP_DURATION_US = 200_000


def _segment_windows_us(project: ProjectState) -> list[tuple[int, int, int]]:
    """Per segment: (source inPoint µs, source outPoint µs, timeline duration µs)."""
    windows: list[tuple[int, int, int]] = []
    for seg in project.segments:
        spec = parse_trim_times(seg.start_text, seg.end_text)
        start_us = _us(spec.start_seconds)
        end_us = _us(spec.end_seconds)
        windows.append((start_us, end_us, end_us - start_us))
    return windows


def _joined_reel_filename(fn: str) -> bool:
    lower = fn.lower()
    return any(marker.lower() in lower for marker in _JOINED_REEL_MARKERS)


def _source_segment_filename(fn: str) -> bool:
    if not _VIDEO_FILE_RE.search(fn):
        return False
    if _IMAGE_FILE_RE.search(fn):
        return False
    if _joined_reel_filename(fn):
        return False
    return True


def _video_clips_in_track(clips: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        clip
        for clip in clips
        if _VIDEO_FILE_RE.search(str(clip.get("filename", "") or ""))
        and not _is_cover_clip(clip)
        and not _is_title_clip(clip)
    ]


def _is_joined_reel_clip(clip: dict[str, Any], *, track_video_count: int) -> bool:
    if track_video_count > 1:
        return False
    fn = str(clip.get("filename", "") or "")
    if not fn:
        return False
    if _joined_reel_filename(fn):
        return True
    in_pt = int(clip.get("inPoint") or 0)
    out_pt = int(clip.get("outPoint") or 0)
    # Template joined export: one clip spanning nearly the full reel (in ≈ 0).
    return in_pt == 0 and out_pt > 300_000_000 and _VIDEO_FILE_RE.search(fn) is not None


def _is_source_segment_clip(clip: dict[str, Any], *, track_video_count: int) -> bool:
    if track_video_count <= 1:
        return False
    fn = str(clip.get("filename", "") or "")
    return bool(fn) and _VIDEO_FILE_RE.search(fn) and not _is_cover_clip(clip)


def _source_segment_clips_in_track(
    clips: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    video_clips = _video_clips_in_track(clips)
    return [
        clip
        for clip in video_clips
        if _is_source_segment_clip(clip, track_video_count=len(video_clips))
        and not _joined_reel_filename(str(clip.get("filename", "") or ""))
    ]


def _renumber_timeline_clip_uid(clip: dict[str, Any]) -> None:
    """Assign fresh ids to the clip and every nested effect/transition (required when cloning slots)."""

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            if "thisUId" in obj and isinstance(obj["thisUId"], str):
                obj["thisUId"] = _new_timeline_clip_uid()
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(clip)


def _assign_unique_nested_uids(obj: Any, seen: set[str]) -> None:
    """Renumber nested ``thisUId`` values, replacing any id already used on this track."""
    if isinstance(obj, dict):
        if "thisUId" in obj and isinstance(obj["thisUId"], str):
            uid = obj["thisUId"]
            if uid in seen:
                obj["thisUId"] = _new_timeline_clip_uid()
            seen.add(str(obj["thisUId"]))
        for value in obj.values():
            _assign_unique_nested_uids(value, seen)
    elif isinstance(obj, list):
        for item in obj:
            _assign_unique_nested_uids(item, seen)


def _strip_clip_transitions(clip: dict[str, Any]) -> None:
    """Remove template transitions whose tl range no longer matches retimed clips."""
    clip.pop("postTransition", None)
    clip.pop("preTransition", None)


def _sync_clip_speed_span(
    clip: dict[str, Any],
    *,
    duration_tl_units: int,
    time_scale: int,
) -> None:
    """Match speed.offsetEnd to the retimed clip span (seconds).

    Leave speedParam._totalTime at the template's full-source value — shrinking it
    freezes every segment in Filmora.
    """
    speed = clip.get("speed")
    if not isinstance(speed, dict):
        return
    duration_s = max(duration_tl_units / time_scale, 0.001)
    speed["offset"] = 0.0
    speed["offsetEnd"] = duration_s


def _align_extra_segment_slots_from_first(segment_clips: list[dict[str, Any]]) -> None:
    """Use slot 0 clip shape for extras — template slot 2+ breaks audio after retiming."""
    if len(segment_clips) <= 2:
        return
    prototype = segment_clips[0]
    for idx in range(1, len(segment_clips)):
        clip = segment_clips[idx]
        replacement = copy.deepcopy(prototype)
        _renumber_timeline_clip_uid(replacement)
        _strip_clip_transitions(replacement)
        clip.clear()
        clip.update(replacement)


def _ensure_source_segment_clip_count(
    track: dict[str, Any],
    *,
    count: int,
) -> list[dict[str, Any]]:
    """Grow mirrored sermon segment slots when the user added more trims than the template."""
    clips: list[dict[str, Any]] = list(track.get("clipList") or [])
    segment_clips = _source_segment_clips_in_track(clips)
    if not segment_clips or count <= len(segment_clips):
        return segment_clips
    prototype = segment_clips[0]
    added = 0
    while len(segment_clips) < count:
        new_clip = copy.deepcopy(prototype)
        _renumber_timeline_clip_uid(new_clip)
        _strip_clip_transitions(new_clip)
        try:
            insert_at = max(clips.index(clip) for clip in segment_clips) + 1
        except ValueError:
            insert_at = len(clips)
        clips.insert(insert_at, new_clip)
        segment_clips.append(new_clip)
        added += 1
    track["clipList"] = clips
    if added:
        log_info("export", f"Added {added} sermon segment clip slot(s) on a timeline track")
    return _source_segment_clips_in_track(clips)


def _dedupe_segment_clip_uids_on_track(segment_clips: list[dict[str, Any]]) -> None:
    """Give every segment slot unique nested ids while keeping each slot's template shape."""
    seen: set[str] = set()
    for clip in segment_clips:
        _assign_unique_nested_uids(clip, seen)


def _is_cover_clip(clip: dict[str, Any]) -> bool:
    return _IMAGE_FILE_RE.search(str(clip.get("filename", "") or "")) is not None


def _is_title_clip(clip: dict[str, Any]) -> bool:
    return int(clip.get("type") or 0) == CLIP_TYPE_TITLE


def _is_music_clip(clip: dict[str, Any]) -> bool:
    return _AUDIO_FILE_RE.search(str(clip.get("filename", "") or "")) is not None


def _uses_tl_source_trim_style(clip: dict[str, Any]) -> bool:
    """True when trim range is encoded in tlBegin/tlEnd with inPoint=0/out≈full file."""
    in_pt = int(clip.get("inPoint") or 0)
    out_pt = int(clip.get("outPoint") or 0)
    tl_begin = int(clip.get("tlBegin") or 0)
    tl_end = int(clip.get("tlEnd") or 0)
    tl_span = max(0, tl_end - tl_begin)
    if in_pt != 0 or tl_span <= 0:
        return False
    return out_pt > tl_span * 4


def _detect_timeline_time_scale(data: dict[str, Any]) -> int:
    """
    Timeline clip fields use either microseconds (1e6/s) or 100 ns units (1e7/s).

    New sermon-highlights exports keep resources in µs but clip trims in 100 ns.
    """
    res_len = 0
    for res in data.get("resources", []):
        if _VIDEO_FILE_RE.search(str(res.get("filename", "") or "")):
            res_len = max(res_len, int(res.get("mediaLength") or 0))
            break
    for timeline_info in data.get("timelineInfos", []):
        for track in timeline_info.get("trackInfos", []):
            for clip in track.get("clipList") or []:
                if not _VIDEO_FILE_RE.search(str(clip.get("filename", "") or "")):
                    continue
                out_pt = int(clip.get("outPoint") or 0)
                span = max(
                    0,
                    int(clip.get("tlEnd") or 0) - int(clip.get("tlBegin") or 0),
                )
                if span > 0 and res_len > out_pt * 8:
                    return 10_000_000
                return 1_000_000
    return 1_000_000


def _to_timeline_units(duration_us: int, time_scale: int) -> int:
    """Convert internal µs values to timeline clip time units."""
    return (max(0, duration_us) * time_scale) // 1_000_000


def _detect_timeline_trim_style(data: dict[str, Any]) -> str:
    """
    Return ``tl_source_trim`` or ``source_in_out``.

    - Legacy: inPoint=0, outPoint≈full file, trim in tlBegin/tlEnd.
    - Current sermon-highlights: inPoint/outPoint = source trim, tlBegin/tlEnd = timeline.
    """
    for timeline_info in data.get("timelineInfos", []):
        for track in timeline_info.get("trackInfos", []):
            video_clips = _video_clips_in_track(track.get("clipList") or [])
            if len(video_clips) >= 1 and _uses_tl_source_trim_style(video_clips[0]):
                return "tl_source_trim"
            if video_clips:
                return "source_in_out"
    return "source_in_out"


def _remove_unused_segment_clips(
    track: dict[str, Any],
    segment_clips: list[dict[str, Any]],
    *,
    windows: list[tuple[int, int, int]],
) -> list[dict[str, Any]]:
    """Drop sermon-segment slots beyond what the user actually trimmed.

    ``sermon-highlights.wfp`` ships with multiple mirrored sermon clips per track.
    When the user requests fewer trims than the template carries, every extra slot
    becomes a stub. Parking them at zero span (the old behaviour) makes both
    Filmora and Wordly's own audit treat the project as corrupt, and copying the
    same range into every slot duplicates the segment on the timeline. Removing
    the extras keeps the timeline exactly as long as the user asked for,
    regardless of how many slots the template happened to bundle.
    """
    keep = len(windows)
    if len(segment_clips) <= keep:
        return segment_clips
    extras_ids = {id(clip) for clip in segment_clips[keep:]}
    clips: list[dict[str, Any]] = track.get("clipList") or []
    track["clipList"] = [clip for clip in clips if id(clip) not in extras_ids]
    return segment_clips[:keep]


def _collapse_extra_segment_clip(
    clip: dict[str, Any],
    *,
    source_url: str,
    source_duration_us: int,
    at_source_us: int,
) -> None:
    clip["filename"] = source_url
    clip["inPoint"] = 0
    clip["outPoint"] = source_duration_us
    clip["tlBegin"] = at_source_us
    clip["tlEnd"] = at_source_us


def _apply_source_segment_clips(
    clips: list[dict[str, Any]],
    *,
    windows: list[tuple[int, int, int]],
    source_url: str,
    source_duration_us: int = 0,
    trim_style: str = "source_in_out",
    time_scale: int = 1_000_000,
) -> int:
    """Lay sermon segments on timeline tracks (mirrored Video 1 / Video 2 layout)."""
    if not windows:
        return 0
    scaled_source_end = _to_timeline_units(source_duration_us, time_scale)
    if trim_style == "tl_source_trim":
        for idx, clip in enumerate(clips):
            if idx >= len(windows):
                continue
            in_pt, out_pt, _dur_us = windows[idx]
            clip["filename"] = source_url
            clip["inPoint"] = 0
            clip["outPoint"] = scaled_source_end
            clip["tlBegin"] = _to_timeline_units(in_pt, time_scale)
            clip["tlEnd"] = _to_timeline_units(out_pt, time_scale)
            _strip_clip_transitions(clip)
            dur_scaled = int(clip["tlEnd"]) - int(clip["tlBegin"])
            _sync_clip_speed_span(clip, duration_tl_units=dur_scaled, time_scale=time_scale)
        return _to_timeline_units(windows[-1][1], time_scale)

    timeline_pos = 0
    for idx, clip in enumerate(clips):
        if idx >= len(windows):
            break
        in_pt, out_pt, dur_us = windows[idx]
        clip["filename"] = source_url
        in_scaled = _to_timeline_units(in_pt, time_scale)
        out_scaled = _to_timeline_units(out_pt, time_scale)
        dur_scaled = _to_timeline_units(dur_us, time_scale)
        clip["inPoint"] = in_scaled
        clip["outPoint"] = out_scaled
        clip["tlBegin"] = timeline_pos
        clip["tlEnd"] = timeline_pos + dur_scaled
        _strip_clip_transitions(clip)
        _sync_clip_speed_span(clip, duration_tl_units=dur_scaled, time_scale=time_scale)
        timeline_pos += dur_scaled
    return timeline_pos


def _max_timeline_end_us(data: dict[str, Any]) -> int:
    end_us = 0
    for timeline_info in data.get("timelineInfos", []):
        for track in timeline_info.get("trackInfos", []):
            for clip in track.get("clipList") or []:
                end_us = max(end_us, int(clip.get("tlEnd") or 0))
    return end_us


def _patch_timeline_track_layout(
    data: dict[str, Any],
    *,
    project: ProjectState,
    layout: TemplateLayout,
    source_path: Path,
    joined_path: Path,
    audio_path: Path | None,
    cover_path: Path | None,
    source_duration_us: int,
    joined_duration_us: int,
    audio_duration_us: int,
) -> int:
    """
    Preserve the template multi-track layout:

    - Title track: verse text for full highlight duration
    - Video tracks: same sermon segments on parallel tracks (one muted in template)
    - Joined-reel tracks: single highlights_joined clip
    - Audio: instrumental bed
    - Cover still at end of a video track
    """
    windows = _segment_windows_us(project)
    trim_style = _detect_timeline_trim_style(data)
    segment_timeline_us = (
        sum(duration for _, _, duration in windows) if windows else joined_duration_us
    )
    timeline_duration_us = max(segment_timeline_us, joined_duration_us, audio_duration_us, 33_366)

    source_url = _filmora_file_url(source_path)
    joined_url = _filmora_file_url(joined_path)
    audio_url = _filmora_file_url(audio_path) if audio_path else None
    cover_url = _filmora_file_url(cover_path) if cover_path else None

    for timeline_info in data.get("timelineInfos", []):
        for track in timeline_info.get("trackInfos", []):
            clips: list[dict[str, Any]] = track.get("clipList") or []
            if not clips:
                continue

            video_clips = _video_clips_in_track(clips)
            segment_clips = _ensure_source_segment_clip_count(
                track, count=len(windows)
            )
            _align_extra_segment_slots_from_first(segment_clips)
            segment_clips = _remove_unused_segment_clips(
                track, segment_clips, windows=windows
            )
            clips = track.get("clipList") or []
            if segment_clips and windows:
                for clip in segment_clips:
                    clip["filename"] = source_url
                _apply_source_segment_clips(
                    segment_clips,
                    windows=windows,
                    source_url=source_url,
                    source_duration_us=source_duration_us,
                    trim_style=trim_style,
                )
                _dedupe_segment_clip_uids_on_track(segment_clips)

            for clip in clips:
                if _is_joined_reel_clip(clip, track_video_count=len(video_clips)):
                    clip["filename"] = joined_url
                    clip["inPoint"] = 0
                    clip["outPoint"] = joined_duration_us
                    clip["tlBegin"] = 0
                    clip["tlEnd"] = min(joined_duration_us, segment_timeline_us or joined_duration_us)

                elif _is_music_clip(clip) and audio_url:
                    clip["filename"] = audio_url
                    clip["inPoint"] = 0
                    if trim_style == "tl_source_trim":
                        end_us = max(joined_duration_us, 33_366)
                    else:
                        end_us = (
                            min(audio_duration_us, timeline_duration_us)
                            if audio_duration_us
                            else timeline_duration_us
                        )
                        end_us = max(end_us, 33_366)
                    clip["outPoint"] = end_us
                    if int(clip.get("tlBegin") or 0) == 0:
                        clip["tlEnd"] = end_us

                elif _is_cover_clip(clip):
                    if cover_url:
                        clip["filename"] = cover_url
                    if trim_style == "tl_source_trim":
                        cover_end = segment_timeline_us or joined_duration_us
                        cover_start = max(0, cover_end - COVER_CLIP_DURATION_US)
                        clip["inPoint"] = 0
                        clip["outPoint"] = COVER_CLIP_DURATION_US
                        clip["tlBegin"] = cover_start
                        clip["tlEnd"] = cover_end
                    else:
                        cover_start = max(0, segment_timeline_us - COVER_CLIP_DURATION_US)
                        clip["inPoint"] = 0
                        clip["outPoint"] = COVER_CLIP_DURATION_US
                        clip["tlBegin"] = cover_start
                        clip["tlEnd"] = segment_timeline_us

                elif _is_title_clip(clip):
                    end_us = segment_timeline_us or joined_duration_us
                    clip["inPoint"] = 0
                    clip["outPoint"] = (
                        source_duration_us if trim_style == "tl_source_trim" else end_us
                    )
                    clip["tlBegin"] = 0
                    clip["tlEnd"] = end_us

                elif trim_style == "tl_source_trim" and int(clip.get("type") or 0) == 4:
                    end_us = segment_timeline_us or joined_duration_us
                    clip["inPoint"] = 0
                    clip["outPoint"] = end_us
                    clip["tlBegin"] = 0
                    clip["tlEnd"] = end_us

            _sync_clip_paths_to_source_uuid(
                clips,
                layout=layout,
                source_url=source_url,
                joined_url=joined_url,
            )

    _repair_timeline_clips_json(
        data,
        project=project,
        source_duration_us=source_duration_us,
        joined_duration_us=joined_duration_us,
        audio_duration_us=audio_duration_us,
        timeline_duration_us=timeline_duration_us,
        trim_style=trim_style,
    )
    return timeline_duration_us


def _patch_timeline_segments_only(
    data: dict[str, Any],
    *,
    project: ProjectState,
    source_url: str,
    source_duration_us: int,
) -> int:
    """
    sermon-highlights.wfp opens when the template clip layout is preserved.

    Retime each user segment and add timeline slots when there are more trims than the
    template shipped with. Leave music, title, cover, and unused slots collapsed.
    """
    windows = _segment_windows_us(project)
    if not windows:
        return 0
    trim_style = _detect_timeline_trim_style(data)
    time_scale = _detect_timeline_time_scale(data)
    timeline_end = 0
    for timeline_info in data.get("timelineInfos", []):
        for track in timeline_info.get("trackInfos", []):
            clips: list[dict[str, Any]] = track.get("clipList") or []
            if not clips:
                continue
            segment_clips = _ensure_source_segment_clip_count(
                track, count=len(windows)
            )
            _align_extra_segment_slots_from_first(segment_clips)
            segment_clips = _remove_unused_segment_clips(
                track, segment_clips, windows=windows
            )
            if segment_clips:
                for clip in segment_clips:
                    clip["filename"] = source_url
                track_end = _apply_source_segment_clips(
                    segment_clips,
                    windows=windows,
                    source_url=source_url,
                    source_duration_us=source_duration_us,
                    trim_style=trim_style,
                    time_scale=time_scale,
                )
                _dedupe_segment_clip_uids_on_track(segment_clips)
                timeline_end = max(timeline_end, track_end)
    return timeline_end


def _rewrite_timeline_joined_filenames(
    text: str, joined_path: Path, path_markers: tuple[str, ...]
) -> str:
    """Point only joined-reel template filenames at highlights_joined (not sermon segments)."""
    target_url = _filmora_file_url(joined_path)

    def repl(match: re.Match[str]) -> str:
        current = match.group(1)
        if not _VIDEO_FILE_RE.search(current):
            return match.group(0)
        if _joined_reel_filename(current) or any(
            marker in current for marker in path_markers if _joined_reel_filename(marker)
        ):
            return f'"filename":"{target_url}"'
        return match.group(0)

    return re.sub(r'"filename":"(file:[^"]+)"', repl, text)


def _patch_timeline_music_clips(
    data: dict[str, Any],
    *,
    audio_path: Path | None,
    audio_duration_us: int,
    highlight_duration_us: int,
) -> None:
    """Point timeline music clips at the exported instrumental.

    ``resources[]`` is not enough — Filmora reads ``clipList[].filename`` on the audio track.
    """
    if audio_path is None:
        return
    audio_url = _filmora_file_url(audio_path)
    time_scale = _detect_timeline_time_scale(data)
    end_tl = max(_to_timeline_units(highlight_duration_us, time_scale), 33_366)
    if audio_duration_us > 0:
        end_tl = min(end_tl, _to_timeline_units(audio_duration_us, time_scale))

    audio_uuid = ""
    for res in data.get("resources", []):
        if _AUDIO_FILE_RE.search(str(res.get("filename", "") or "")):
            audio_uuid = str(res.get("sourceUuid", "") or "")
            break

    for timeline_info in data.get("timelineInfos", []):
        for track in timeline_info.get("trackInfos", []):
            for clip in track.get("clipList") or []:
                clip_uuid = str(clip.get("sourceUuid", "") or "")
                if not _is_music_clip(clip) and clip_uuid != audio_uuid:
                    continue
                clip["filename"] = audio_url
                clip["inPoint"] = 0
                clip["outPoint"] = end_tl
                if int(clip.get("tlBegin") or 0) == 0:
                    clip["tlEnd"] = end_tl


def _patch_timeline_resources(
    data: dict[str, Any],
    *,
    source_path: Path,
    joined_path: Path,
    audio_path: Path | None,
    cover_path: Path | None,
    source_duration_us: int,
    joined_duration_us: int,
    audio_duration_us: int,
) -> None:
    """Sync timeline.resources[] with exported sermon, joined reel, music, and cover."""
    source_url = _filmora_file_url(source_path)
    joined_url = _filmora_file_url(joined_path)
    audio_url = _filmora_file_url(audio_path) if audio_path else None
    cover_url = _filmora_file_url(cover_path) if cover_path else None

    for res in data.get("resources", []):
        fn = str(res.get("filename", ""))
        lower = fn.lower()
        if audio_url and _AUDIO_FILE_RE.search(lower):
            res["filename"] = audio_url
            res["mediaLength"] = audio_duration_us
        elif cover_url and _IMAGE_FILE_RE.search(lower):
            res["filename"] = cover_url
            res["mediaLength"] = COVER_CLIP_DURATION_US
        elif _joined_reel_filename(fn):
            res["filename"] = joined_url
            res["mediaLength"] = joined_duration_us
        elif _VIDEO_FILE_RE.search(fn):
            res["filename"] = source_url
            res["mediaLength"] = source_duration_us
        elif _IMAGE_FILE_RE.search(lower):
            existing = int(res.get("mediaLength") or 0)
            if existing <= 0 or existing > 60_000_000:
                res["mediaLength"] = COVER_CLIP_DURATION_US


def _apply_replacements_to_extract_dir(extract_dir: Path, replacements: dict[str, str]) -> None:
    for path in extract_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.name == "timeline.wesproj":
            # Timeline paths are updated via structured JSON; blind replace corrupts
            # resources[] and clip filename URLs.
            continue
        if path.suffix.lower() in {".png", ".fsthumb"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        patched = _replace_paths_in_text(text, replacements)
        if patched != text:
            path.write_text(patched, encoding="utf-8")


def _source_video_path(_project: ProjectState, joined_path: Path) -> Path:
    """Timeline uses one joined highlight reel — do not re-link the full sermon download."""
    return joined_path.resolve()


def _segment_durations_us(project: ProjectState) -> list[int]:
    durations: list[int] = []
    for seg in project.segments:
        spec = parse_trim_times(seg.start_text, seg.end_text)
        durations.append(_us(spec.duration_seconds))
    return durations


def _escape_wesproj_text(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\r")
    )


def _reference_verse_timeline_info() -> dict[str, Any]:
    if REFERENCE_VERSE_TIMELINE_INFO_PATH.is_file():
        return copy.deepcopy(
            json.loads(REFERENCE_VERSE_TIMELINE_INFO_PATH.read_text(encoding="utf-8"))
        )
    raise FileNotFoundError(
        f"Missing {REFERENCE_VERSE_TIMELINE_INFO_PATH.name} — save a working Filmora "
        "subtitle timeline block into assets/filmora_templates/."
    )


def _reference_verse_script_clip() -> dict[str, Any]:
    if REFERENCE_VERSE_SCRIPT_CLIP_PATH.is_file():
        return copy.deepcopy(
            json.loads(REFERENCE_VERSE_SCRIPT_CLIP_PATH.read_text(encoding="utf-8"))
        )
    raise FileNotFoundError(
        f"Missing {REFERENCE_VERSE_SCRIPT_CLIP_PATH.name} — save a working Filmora "
        "subtitle clip into assets/filmora_templates/."
    )


def _new_timeline_clip_uid() -> str:
    return str(uuid.uuid4())


def _verse_timeline_duration_us(project: ProjectState, *, joined_duration_us: int) -> int:
    windows = _segment_windows_us(project)
    if windows:
        return sum(duration for _, _, duration in windows)
    return joined_duration_us


def _patch_script_buf_verse(script_buf: str, reference: str, body: str) -> str:
    """Update Filmora subtitle JSON embedded in scriptBuf (Text + CharData)."""
    full = reference + "\r" + body
    inner_escaped = json.dumps(full, ensure_ascii=False)[1:-1]
    for field in ("CharData", "Text"):
        marker = f'"{field}":"'
        if marker not in script_buf:
            continue
        script_buf = re.sub(
            rf'"{field}":"(?:\\.|[^"\\])*"',
            f'"{field}":"{inner_escaped}"',
            script_buf,
            count=1,
        )
    return script_buf


def _apply_verse_script_clip(
    clip: dict[str, Any],
    *,
    reference: str,
    body: str,
    duration_tl: int,
) -> None:
    clip["inPoint"] = 0
    clip["outPoint"] = duration_tl
    clip["tlBegin"] = 0
    clip["tlEnd"] = duration_tl
    _strip_clip_transitions(clip)
    script_buf = str(clip.get("scriptBuf") or "")
    if script_buf:
        clip["scriptBuf"] = _patch_script_buf_verse(script_buf, reference, body)
        clip["scriptBufSize"] = len(clip["scriptBuf"])


def _ensure_verse_script_layer(
    data: dict[str, Any],
    *,
    project: ProjectState,
    joined_duration_us: int,
    time_scale: int,
) -> None:
    """Add the Bible verse on Filmora's dedicated subtitle timeline (timelineInfos[1])."""
    verse = project.selected_verse
    if not verse:
        return

    duration_us = _verse_timeline_duration_us(project, joined_duration_us=joined_duration_us)
    duration_tl = max(_to_timeline_units(duration_us, time_scale), 33_366)

    timeline_infos: list[dict[str, Any]] = data.get("timelineInfos") or []
    if len(timeline_infos) < 2:
        # sermon-highlights has one compound timeline; injecting a script clip on track 6
        # or timelineInfos[1] breaks Filmora project load. Verse text is in verse.txt.
        log_info(
            "export",
            "Verse subtitle skipped (template has no subtitle timeline block)",
        )
        return

    verse_timeline = timeline_infos[1]
    tracks: list[dict[str, Any]] = verse_timeline.setdefault("trackInfos", [])
    if not tracks:
        tracks.append({"clipList": []})

    verse_track = tracks[0]
    clip: dict[str, Any] | None = None
    for candidate in verse_track.get("clipList") or []:
        if int(candidate.get("type") or 0) == CLIP_TYPE_SCRIPT and candidate.get("scriptBuf"):
            clip = candidate
            break

    if clip is None:
        clip = _reference_verse_script_clip()
        _renumber_timeline_clip_uid(clip)
        verse_track["clipList"] = [clip]

    _apply_verse_script_clip(
        clip,
        reference=verse.reference,
        body=verse.text,
        duration_tl=duration_tl,
    )

    log_info(
        "export",
        f"Verse subtitle layer on timelineInfos[1] ({duration_tl} timeline units)",
    )


def _patch_timeline_verse_text(text: str, reference: str, body: str) -> str:
    ref = _escape_wesproj_text(reference)
    verse_body = _escape_wesproj_text(body)
    full = ref + "\\r" + verse_body

    def _escaped_text_repl(_match: re.Match[str]) -> str:
        # Use a callable so re.sub does not treat \\r in verse text as a control escape.
        return f'\\"Text\\":\\"{full}\\"'

    if re.search(r'\\"Text\\":\\"', text):
        text = re.sub(r'\\"Text\\":\\"[^"]*\\"', _escaped_text_repl, text, count=1)

    char_re = re.compile(r'\\"CharData\\":\\"((?:[^"\\]|\\.)*)\\"')
    char_parts = list(char_re.finditer(text))
    if len(char_parts) >= 2:
        start, end = char_parts[0].span()
        text = text[:start] + f'\\"CharData\\":\\"{ref}\\"' + text[end:]
        char_parts = list(char_re.finditer(text))
        start, end = char_parts[1].span()
        text = text[:start] + f'\\"CharData\\":\\"\\r{verse_body}\\"' + text[end:]
    elif char_parts:
        start, end = char_parts[0].span()
        text = text[:start] + f'\\"CharData\\":\\"{full}\\"' + text[end:]
    return text


_IN_OUT_RE = re.compile(r'"inPoint":(\d+),"outPoint":(\d+)')
# Clips keep filename within a few hundred bytes before inPoint (not top-level resources).
_LOOKBACK_WINDOW = 8192
_LOOKAHEAD_WINDOW = 8192
_MAX_FILENAME_TO_INPOINT = 8192


def _filename_before(text: str, start: int) -> str:
    window_start = max(0, start - _LOOKBACK_WINDOW)
    window = text[window_start:start]
    matches = list(re.finditer(r'"filename":"(file:[^"]+)"', window))
    if not matches:
        return ""
    last = matches[-1]
    if start - (window_start + last.end()) > _MAX_FILENAME_TO_INPOINT:
        return ""
    return last.group(1)


def _filename_after(text: str, end: int) -> str:
    window_end = min(len(text), end + _LOOKAHEAD_WINDOW)
    window = text[end:window_end]
    match = re.search(r'"filename":"(file:[^"]+)"', window)
    if not match:
        return ""
    if match.start() > _MAX_FILENAME_TO_INPOINT:
        return ""
    return match.group(1)


def _filename_near(text: str, pos: int) -> str:
    return _filename_before(text, pos) or _filename_after(text, pos)


def _patch_timeline_wesproj(
    text: str,
    *,
    project: ProjectState,
    layout: TemplateLayout,
    source_path: Path,
    joined_path: Path,
    audio_path: Path | None,
    cover_path: Path | None,
    source_duration_us: int,
    joined_duration_us: int,
    audio_duration_us: int,
    timeline_duration_us: int,
) -> str:
    """
    Realign the template timeline to Wordly sermon segments while keeping Filmora's
    multi-track layout (title, mirrored source video tracks, joined reel, music).
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None

    trim_style: str | None = None
    if data is not None:
        trim_style = _detect_timeline_trim_style(data)
        source_url = _filmora_file_url(source_path)
        time_scale = _detect_timeline_time_scale(data)
        if trim_style in ("tl_source_trim", "source_in_out"):
            patched_duration = _patch_timeline_segments_only(
                data,
                project=project,
                source_url=source_url,
                source_duration_us=source_duration_us,
            )
            log_info(
                "export",
                f"Timeline patch: segment slots only ({trim_style}, scale={time_scale})",
            )
        else:
            patched_duration = _patch_timeline_track_layout(
                data,
                project=project,
                layout=layout,
                source_path=source_path,
                joined_path=joined_path,
                audio_path=audio_path,
                cover_path=cover_path,
                source_duration_us=source_duration_us,
                joined_duration_us=joined_duration_us,
                audio_duration_us=audio_duration_us,
            )
            log_info("export", f"Timeline patch: full layout ({trim_style})")
        _patch_timeline_resources(
            data,
            source_path=source_path,
            joined_path=joined_path,
            audio_path=audio_path,
            cover_path=cover_path,
            source_duration_us=source_duration_us,
            joined_duration_us=joined_duration_us,
            audio_duration_us=audio_duration_us,
        )
        windows = _segment_windows_us(project)
        highlight_duration_us = (
            sum(duration for _, _, duration in windows) if windows else joined_duration_us
        )
        _patch_timeline_music_clips(
            data,
            audio_path=audio_path,
            audio_duration_us=audio_duration_us,
            highlight_duration_us=highlight_duration_us,
        )
        timeline_duration_us = max(timeline_duration_us, patched_duration)
        if project.selected_verse:
            time_scale = _detect_timeline_time_scale(data)
            _ensure_verse_script_layer(
                data,
                project=project,
                joined_duration_us=joined_duration_us,
                time_scale=time_scale,
            )
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    text = _repair_timeline_inouts(
        text,
        project=project,
        source_duration_us=source_duration_us,
        joined_duration_us=joined_duration_us,
        audio_duration_us=audio_duration_us,
        timeline_duration_us=timeline_duration_us,
        trim_style=trim_style,
    )

    verse = project.selected_verse
    if verse and trim_style not in ("tl_source_trim", "source_in_out"):
        text = _patch_timeline_verse_text(text, verse.reference, verse.text)

    return text


def _repair_timeline_clips_json(
    data: dict[str, Any],
    *,
    project: ProjectState,
    source_duration_us: int,
    joined_duration_us: int,
    audio_duration_us: int,
    timeline_duration_us: int,
    trim_style: str | None = None,
) -> None:
    """Fix broken clip trims on timeline tracks only (never effect-chain in/out fields)."""
    if trim_style is None:
        trim_style = _detect_timeline_trim_style(data)
    if trim_style in ("tl_source_trim", "source_in_out"):
        return
    windows = _segment_windows_us(project)
    absolute_cap = max(source_duration_us, joined_duration_us, 3_600_000_000)

    for timeline_info in data.get("timelineInfos", []):
        for track in timeline_info.get("trackInfos", []):
            clips: list[dict[str, Any]] = track.get("clipList") or []
            video_count = len(_video_clips_in_track(clips))
            segment_clips = [
                clip
                for clip in _video_clips_in_track(clips)
                if _is_source_segment_clip(clip, track_video_count=video_count)
                and not _joined_reel_filename(str(clip.get("filename", "") or ""))
            ]

            if segment_clips and windows and trim_style != "tl_source_trim":
                _apply_source_segment_clips(
                    segment_clips,
                    windows=windows,
                    source_url=str(segment_clips[0].get("filename", "")),
                    source_duration_us=source_duration_us,
                    trim_style=trim_style,
                )

            for clip in clips:
                in_pt = int(clip.get("inPoint") or 0)
                out_pt = int(clip.get("outPoint") or 0)
                if trim_style == "tl_source_trim" and _VIDEO_FILE_RE.search(
                    str(clip.get("filename", "") or "")
                ):
                    if in_pt == 0 and out_pt >= source_duration_us:
                        continue
                if out_pt - in_pt > 1 and in_pt <= absolute_cap and out_pt <= absolute_cap:
                    continue

                fn = str(clip.get("filename", "") or "")
                if _is_title_clip(clip):
                    end = sum(duration for _, _, duration in windows) or joined_duration_us
                    clip["inPoint"] = 0
                    clip["outPoint"] = end
                    clip["tlBegin"] = 0
                    clip["tlEnd"] = end
                elif _is_music_clip(clip):
                    end = min(audio_duration_us, timeline_duration_us) if audio_duration_us else timeline_duration_us
                    clip["inPoint"] = 0
                    clip["outPoint"] = max(end, 33_366)
                elif _is_cover_clip(clip):
                    clip["inPoint"] = 0
                    clip["outPoint"] = COVER_CLIP_DURATION_US
                elif _is_joined_reel_clip(clip, track_video_count=video_count):
                    end = min(joined_duration_us, timeline_duration_us)
                    clip["inPoint"] = 0
                    clip["outPoint"] = max(end, 33_366)
                elif _VIDEO_FILE_RE.search(fn) and windows and segment_clips:
                    continue
                elif _VIDEO_FILE_RE.search(fn):
                    end = min(source_duration_us, timeline_duration_us)
                    clip["inPoint"] = 0
                    clip["outPoint"] = max(end, 33_366)


def _repair_timeline_inouts(
    text: str,
    *,
    project: ProjectState,
    source_duration_us: int,
    joined_duration_us: int,
    audio_duration_us: int,
    timeline_duration_us: int,
    trim_style: str | None = None,
) -> str:
    """Repair clip trims via structured JSON when possible (safe for multi-track layouts)."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text
    if trim_style is None:
        trim_style = _detect_timeline_trim_style(data)
    if trim_style in ("tl_source_trim", "source_in_out"):
        return text
    _repair_timeline_clips_json(
        data,
        project=project,
        source_duration_us=source_duration_us,
        joined_duration_us=joined_duration_us,
        audio_duration_us=audio_duration_us,
        timeline_duration_us=timeline_duration_us,
        trim_style=trim_style,
    )
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _patch_project_info(
    data: dict[str, Any],
    project: ProjectState,
    output_path: Path,
    duration_us: int,
    *,
    cover_path: Path | None = None,
    preserve_project_guid: bool = False,
) -> None:
    now = int(time.time())
    win_out = _filmora_path_str(output_path)
    old_guid = str(data.get("project_guid", ""))
    if not preserve_project_guid:
        data["project_guid"] = _guid()
    # Keep the template Backup/*.wfpbundle cover path when preserving the project identity
    # (sermon-highlights opens reliably with it). Only clear a raw .jpg path.
    backup_cover = str(data.get("proj_cover_proj_path", "") or "")
    if not preserve_project_guid:
        if old_guid and old_guid in backup_cover:
            data["proj_cover_proj_path"] = ""
        elif backup_cover.lower().endswith((".jpg", ".jpeg", ".png")):
            data["proj_cover_proj_path"] = ""
    data["project_file_name"] = project.project_name
    data["project_date_create"] = now
    data["project_date_modify"] = now
    data["project_editor_create_version"] = FILMORA_BUILD
    data["project_editor_modify_version"] = FILMORA_BUILD
    data["project_timeline_duration"] = duration_us
    data["proj_zip_save_path"] = win_out


def _refresh_medias_info_md5(data: dict[str, Any]) -> None:
    """Recompute src_md5 from download_url so Filmora registers media in Project Media."""
    for item in data.get("media_items", {}).values():
        url = str(item.get("download_url", "") or "")
        if not url or url.startswith("{"):
            continue
        local = Path(url.replace("/", "\\"))
        if local.is_file():
            item["src_md5"] = _media_file_md5(local)


def _assign_media_item(
    item: dict[str, Any],
    *,
    new_path: Path,
    duration_us: int,
    replacements: dict[str, str],
) -> None:
    old = str(item.get("download_url", "") or item.get("file_name", ""))
    new = _filmora_path_str(new_path)
    item["download_url"] = new
    item["name"] = new_path.stem[:80]
    item["media_length"] = duration_us
    item["mark_info_list"] = [{"mark_in": -1, "mark_out": -1}]
    item["src_md5"] = _media_file_md5(new_path)
    _register_replacement(replacements, old, new)


def _patch_medias_info(
    data: dict[str, Any],
    project: ProjectState,
    layout: TemplateLayout,
    *,
    video_path: Path,
    source_video_path: Path,
    audio_path: Path | None,
    cover_path: Path | None,
    video_duration_us: int,
    source_duration_us: int,
    audio_duration_us: int,
    timeline_duration_us: int = 0,
) -> dict[str, str]:
    """Update every template video/audio registry entry; return old→new replacements."""
    replacements: dict[str, str] = {}
    items: dict[str, Any] = data.get("media_items", {})

    for media_id, item in items.items():
        media_type = int(item.get("media_type", 0))
        url = str(item.get("download_url", "") or "")
        if url and not url.startswith("file:"):
            local = Path(url.replace("/", "\\"))
            if local.is_file():
                item["src_md5"] = _media_file_md5(local)

        if media_id == layout.timeline_id:
            item["duration"] = timeline_duration_us or max(
                video_duration_us, audio_duration_us, source_duration_us
            )
            if project.project_name:
                item["name"] = project.project_name[:80]
            continue

        if media_type == MEDIA_TYPE_VIDEO:
            if media_id == layout.source_video_id:
                target = source_video_path
                duration = source_duration_us
            elif media_id == layout.joined_video_id:
                target = video_path
                duration = video_duration_us
            else:
                target = source_video_path
                duration = source_duration_us
            _assign_media_item(
                item,
                new_path=target,
                duration_us=duration,
                replacements=replacements,
            )
        elif media_type == MEDIA_TYPE_AUDIO and audio_path is not None:
            _assign_media_item(
                item,
                new_path=audio_path,
                duration_us=audio_duration_us,
                replacements=replacements,
            )
        elif media_type == MEDIA_TYPE_IMAGE and cover_path is not None:
            _assign_media_item(
                item,
                new_path=cover_path,
                duration_us=500_000,
                replacements=replacements,
            )

    data["media_items"] = items
    return replacements


def _patch_media_json_files(
    extract_dir: Path,
    layout: TemplateLayout,
    *,
    video_path: Path,
    source_video_path: Path,
    audio_path: Path | None,
    cover_path: Path | None,
    replacements: dict[str, str],
) -> None:
    id_to_path: dict[str, Path] = {}
    if layout.source_video_id:
        id_to_path[layout.source_video_id.strip("{}")] = source_video_path
    if layout.joined_video_id and layout.joined_video_id != layout.source_video_id:
        id_to_path[layout.joined_video_id.strip("{}")] = video_path
    if audio_path is not None:
        for audio_id in layout.audio_ids:
            id_to_path[audio_id.strip("{}")] = audio_path
    if cover_path is not None:
        for image_id in layout.image_ids:
            id_to_path[image_id.strip("{}")] = cover_path

    duration_by_folder: dict[str, int] = {}
    if layout.source_video_id:
        duration_by_folder[layout.source_video_id.strip("{}")] = _us(
            _safe_duration(source_video_path, 60.0)
        )
    if layout.joined_video_id and layout.joined_video_id != layout.source_video_id:
        duration_by_folder[layout.joined_video_id.strip("{}")] = _us(
            _safe_duration(video_path, 60.0)
        )
    if audio_path is not None:
        audio_us = _us(_safe_duration(audio_path, 180.0))
        for audio_id in layout.audio_ids:
            duration_by_folder[audio_id.strip("{}")] = audio_us
    if cover_path is not None:
        for image_id in layout.image_ids:
            duration_by_folder[image_id.strip("{}")] = COVER_CLIP_DURATION_US

    for media_json in extract_dir.glob("ProjectFolder/Medias/*/media.json"):
        folder = media_json.parent.name.strip("{}")
        payload = _read_json(media_json.read_bytes())
        old_name = str(payload.get("file_name", ""))
        duration_us = duration_by_folder.get(folder, 0)

        if folder in id_to_path:
            new_name = _filmora_path_str(id_to_path[folder])
        elif payload.get("sourceInfo", {}).get("basicInfo", {}).get("streamType") == 2:
            # Unknown video slot — default to sermon source (segment template).
            new_name = _filmora_path_str(source_video_path)
            duration_us = duration_us or _us(_safe_duration(source_video_path, 60.0))
        elif audio_path is not None and payload.get("sourceInfo", {}).get("audStreamInfos"):
            new_name = _filmora_path_str(audio_path)
            duration_us = duration_us or _us(_safe_duration(audio_path, 180.0))
        else:
            continue

        payload["file_name"] = new_name
        _sync_media_json_duration(payload, duration_us)
        _register_replacement(replacements, old_name, new_name)
        media_json.write_bytes(_write_json(payload))


def _refresh_media_thumbnails(
    extract_dir: Path,
    *,
    source_video_path: Path,
    cover_path: Path | None,
) -> None:
    """Replace template PNG thumbs with frames from exported media (best-effort)."""
    for media_json in extract_dir.glob("ProjectFolder/Medias/*/media.json"):
        payload = _read_json(media_json.read_bytes())
        file_name = str(payload.get("file_name", "") or "")
        video_path: Path | None = None
        if source_video_path.is_file() and file_name.endswith(
            (".mp4", ".mov", ".m4v")
        ):
            if Path(file_name.replace("/", "\\")).name.lower() == source_video_path.name.lower():
                video_path = source_video_path
        if video_path is None and cover_path and cover_path.is_file():
            if file_name.lower().endswith((".jpg", ".jpeg", ".png")):
                thumb = media_json.parent / "thumbnail.png"
                shutil.copy2(cover_path, thumb)
                continue
        if video_path is None:
            continue
        thumb = media_json.parent / "thumbnail.png"
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-ss",
                    "1",
                    "-i",
                    str(video_path),
                    "-frames:v",
                    "1",
                    str(thumb),
                ],
                check=True,
                capture_output=True,
            )
        except Exception:
            pass


def _sync_timeline_media_duration(
    medias_info: dict[str, Any], timeline_id: str, duration_us: int
) -> None:
    item = medias_info.get("media_items", {}).get(timeline_id)
    if item is not None and duration_us > 0:
        item["duration"] = duration_us


def generate_wfp_from_template(
    project: ProjectState,
    *,
    output_path: Path | None = None,
) -> Path:
    template = template_path()
    log_step("export", f"Using Filmora template: {template}")
    if not template.is_file():
        raise FileNotFoundError(
            f"Filmora 14.2.9 template missing: {template}. "
            "Save a blank project from Filmora as filmora_14_2_9.wfp."
        )

    if not project.joined_clip_path or not project.joined_clip_path.exists():
        raise ValueError("Joined highlight video is required before exporting a Filmora project.")

    EXPORTS.mkdir(parents=True, exist_ok=True)
    stem = _export_stem(project.project_name)
    bundle_dir: Path | None = None
    if output_path is not None:
        resolved_out = output_path.resolve()
        stem = _export_stem(resolved_out.stem)
        bundle_dir = resolved_out.parent
    log_step("export", f"Building export bundle for {stem!r}")
    bundle = _prepare_export_bundle(project, stem=stem, bundle_dir=bundle_dir)
    log_info("export", f"Bundle folder: {bundle.bundle_dir}")
    log_info("export", f"  joined: {bundle.joined.name} ({bundle.joined.stat().st_size // 1024} KiB)")
    log_info("export", f"  source: {bundle.source.name} ({bundle.source.stat().st_size // 1024} KiB)")
    if bundle.music:
        log_info("export", f"  music: {bundle.music.name}")
    if bundle.cover:
        log_info("export", f"  cover: {bundle.cover.name}")

    video_path = bundle.joined
    source_video_path = bundle.source
    audio_path = bundle.music
    cover_path = bundle.cover
    out = bundle.wfp_path

    video_duration_us = _us(_safe_duration(video_path, 60.0))
    source_duration_us = _us(_safe_duration(source_video_path, 60.0))
    audio_duration_us = _us(_safe_duration(audio_path, 180.0)) if audio_path else 0
    log_info(
        "export",
        f"Durations (µs): source={source_duration_us}, joined={video_duration_us}, audio={audio_duration_us}",
    )
    windows = _segment_windows_us(project)
    if windows:
        segment_sum_us = sum(duration for _, _, duration in windows)
        timeline_duration_us = max(
            segment_sum_us, video_duration_us, audio_duration_us, 33_366
        )
    else:
        timeline_duration_us = max(video_duration_us, audio_duration_us)
    _validate_export_segments(
        project,
        source_path=source_video_path,
        joined_path=video_path,
        source_duration_us=source_duration_us,
    )

    extract_dir = TEMP / f"wfp_build_{int(time.time())}"
    extract_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(template) as zf:
        zf.extractall(extract_dir)

    replacements: dict[str, str] = {}

    project_info_path = extract_dir / "ProjectFolder" / "project_info.json"
    medias_info_path = extract_dir / "ProjectFolder" / "Medias" / "medias_info.json"
    project_info = _read_json(project_info_path.read_bytes()) if project_info_path.is_file() else {}
    medias_info = _read_json(medias_info_path.read_bytes()) if medias_info_path.is_file() else {}
    template_timeline_duration_us = int(project_info.get("project_timeline_duration") or 0)
    timeline_id = str(project_info.get("timeline_mediaId", ""))
    timeline_path = extract_dir / "ProjectFolder" / "Medias" / timeline_id / "timeline.wesproj"
    timeline_data: dict[str, Any] | None = None
    if timeline_path.is_file():
        try:
            timeline_data = json.loads(timeline_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            timeline_data = None
    layout = _load_template_layout(project_info, medias_info, timeline_data)
    timeline_path = extract_dir / layout.timeline_rel_path
    if timeline_data is not None:
        layout = _layout_with_timeline_uuids(layout, timeline_data)

    if medias_info_path.is_file():
        replacements.update(
            _patch_medias_info(
                medias_info,
                project,
                layout,
                video_path=video_path,
                source_video_path=source_video_path,
                audio_path=audio_path,
                cover_path=cover_path,
                video_duration_us=video_duration_us,
                source_duration_us=source_duration_us,
                audio_duration_us=audio_duration_us,
                timeline_duration_us=timeline_duration_us,
            )
        )
        medias_info_path.write_bytes(_write_json(medias_info))

    _patch_media_json_files(
        extract_dir,
        layout,
        video_path=video_path,
        source_video_path=source_video_path,
        audio_path=audio_path,
        cover_path=cover_path,
        replacements=replacements,
    )
    timeline_trim_style: str | None = None
    if timeline_path.is_file():
        timeline_text = timeline_path.read_text(encoding="utf-8")
        try:
            timeline_data = json.loads(timeline_text)
            timeline_trim_style = _detect_timeline_trim_style(timeline_data)
            log_info("export", f"Timeline trim style: {timeline_trim_style}")
            layout = _layout_with_timeline_uuids(layout, timeline_data)
            if timeline_trim_style != "tl_source_trim":
                _patch_timeline_resources(
                    timeline_data,
                    source_path=source_video_path,
                    joined_path=video_path,
                    audio_path=audio_path,
                    cover_path=cover_path,
                    source_duration_us=source_duration_us,
                    joined_duration_us=video_duration_us,
                    audio_duration_us=audio_duration_us,
                )
            timeline_text = json.dumps(timeline_data, ensure_ascii=False, separators=(",", ":"))
        except json.JSONDecodeError:
            timeline_text = _replace_paths_in_text(timeline_text, replacements)
        timeline_text = _patch_timeline_wesproj(
            timeline_text,
            project=project,
            layout=layout,
            source_path=source_video_path,
            joined_path=video_path,
            audio_path=audio_path,
            cover_path=cover_path,
            source_duration_us=source_duration_us,
            joined_duration_us=video_duration_us,
            audio_duration_us=audio_duration_us,
            timeline_duration_us=timeline_duration_us,
        )
        timeline_text = _finalize_timeline(
            timeline_text,
            project=project,
            source_duration_us=source_duration_us,
            joined_duration_us=video_duration_us,
            audio_duration_us=audio_duration_us,
            timeline_duration_us=timeline_duration_us,
            trim_style=timeline_trim_style,
        )
        timeline_path.write_text(timeline_text, encoding="utf-8")
        try:
            timeline_duration_us = max(
                timeline_duration_us,
                _max_timeline_end_us(json.loads(timeline_path.read_text(encoding="utf-8"))),
            )
        except json.JSONDecodeError:
            pass

    if timeline_trim_style not in ("tl_source_trim", "source_in_out"):
        _refresh_media_thumbnails(
            extract_dir,
            source_video_path=source_video_path,
            cover_path=cover_path,
        )

    _apply_replacements_to_extract_dir(extract_dir, replacements)

    if timeline_path.is_file():
        timeline_text = timeline_path.read_text(encoding="utf-8")
        timeline_text = _finalize_timeline(
            timeline_text,
            project=project,
            source_duration_us=source_duration_us,
            joined_duration_us=video_duration_us,
            audio_duration_us=audio_duration_us,
            timeline_duration_us=timeline_duration_us,
            trim_style=timeline_trim_style,
        )
        timeline_path.write_text(timeline_text, encoding="utf-8")
        try:
            timeline_duration_us = max(
                timeline_duration_us,
                _max_timeline_end_us(json.loads(timeline_text)),
            )
        except json.JSONDecodeError:
            pass

    if project_info_path.is_file():
        project_info = _read_json(project_info_path.read_bytes())
        windows = _segment_windows_us(project)
        if windows:
            segment_sum_us = sum(duration for _, _, duration in windows)
            timeline_duration_us = max(
                timeline_duration_us,
                segment_sum_us,
                video_duration_us,
                audio_duration_us,
                33_366,
            )
            if (
                timeline_trim_style in ("tl_source_trim", "source_in_out")
                and template_timeline_duration_us > 0
            ):
                time_scale = 1_000_000
                if timeline_data is not None:
                    time_scale = _detect_timeline_time_scale(timeline_data)
                scaled_span = (
                    _to_timeline_units(
                        sum(duration for _, _, duration in windows), time_scale
                    )
                    if windows
                    else 0
                )
                timeline_duration_us = max(
                    timeline_duration_us,
                    template_timeline_duration_us,
                    scaled_span,
                )
            log_info(
                "export",
                f"Segments: {len(windows)} window(s), timeline span={segment_sum_us} µs",
            )
        _patch_project_info(
            project_info,
            project,
            out,
            timeline_duration_us,
            cover_path=cover_path,
            preserve_project_guid=timeline_trim_style
            in ("tl_source_trim", "source_in_out"),
        )
        project_info_path.write_bytes(_write_json(project_info))
        log_info("export", f"proj_cover_proj_path cleared: {not project_info.get('proj_cover_proj_path')}")

    if medias_info_path.is_file():
        medias_info = _read_json(medias_info_path.read_bytes())
        _sync_timeline_media_duration(medias_info, layout.timeline_id, timeline_duration_us)
        _refresh_medias_info_md5(medias_info)
        medias_info_path.write_bytes(_write_json(medias_info))
        log_info("export", f"Timeline media registry duration: {timeline_duration_us} µs")

    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(extract_dir.rglob("*")):
            if file_path.is_file():
                arcname = file_path.relative_to(extract_dir).as_posix()
                zf.write(file_path, arcname)

    project.wfp_output_path = out.resolve()
    log_step("export", f"Wrote {out} ({out.stat().st_size // 1024} KiB)")
    _validate_export_media_paths(out, bundle)
    _validate_video_media_pool_matches_timeline(out, layout)
    notes = audit_export_wfp(out)
    # Only block on truly fatal issues (missing media). Zero-length stub clips
    # used to be created on purpose by Wordly for unused template slots; the
    # export should still go through for any clip count the user picked. Such
    # stragglers are now logged so Filmora can drop them silently when opening.
    fatal = [line for line in notes if line.startswith("MISSING")]
    if fatal:
        raise ValueError(
            "Filmora export failed validation:\n"
            + "\n".join(f"  - {n}" for n in fatal)
        )
    log_export_audit(out)
    return out.resolve()


def _validate_video_media_pool_matches_timeline(wfp_path: Path, layout: TemplateLayout) -> None:
    """
    Filmora rejects projects when media.json points at a different file than timeline clips
    for the same media GUID (common with single-video sermon-highlights templates).
    """
    if not layout.source_video_id:
        return
    media_id = layout.source_video_id.strip("{}")
    media_json_arc = f"ProjectFolder/Medias/{layout.source_video_id}/media.json"
    with zipfile.ZipFile(wfp_path) as zf:
        if media_json_arc not in zf.namelist():
            return
        media_json = _read_json(zf.read(media_json_arc))
        pool_file = Path(str(media_json.get("file_name", "")).replace("/", "\\")).name.lower()
        timeline_id = json.loads(zf.read("ProjectFolder/project_info.json")).get("timeline_mediaId")
        if not timeline_id:
            return
        timeline = json.loads(
            zf.read(f"ProjectFolder/Medias/{timeline_id}/timeline.wesproj").decode("utf-8")
        )
        clip_names: set[str] = set()
        for timeline_info in timeline.get("timelineInfos", []):
            for track in timeline_info.get("trackInfos", []):
                for clip in track.get("clipList") or []:
                    fn = str(clip.get("filename", "") or "")
                    if _VIDEO_FILE_RE.search(fn) and not _IMAGE_FILE_RE.search(fn):
                        clip_names.add(Path(fn.split("/")[-1]).name.lower())
        if not clip_names:
            return
        if pool_file not in clip_names and len(clip_names) == 1:
            only = next(iter(clip_names))
            if pool_file != only:
                raise ValueError(
                    "Filmora project is inconsistent: media pool references "
                    f"{pool_file!r} but timeline video clips use {only!r}. "
                    "Re-export after updating Wordly."
                )


def audit_export_wfp(wfp_path: Path) -> list[str]:
    """Return human-readable audit notes; log them when exporting."""
    issues: list[str] = []
    if not wfp_path.is_file():
        issues.append(f"Missing project file: {wfp_path}")
        return issues
    try:
        with zipfile.ZipFile(wfp_path) as zf:
            pi = json.loads(zf.read("ProjectFolder/project_info.json"))
            issues.append(f"Zip entries: {len(zf.namelist())}")
            issues.append(f"proj_zip_save_path: {pi.get('proj_zip_save_path', '')}")
            issues.append(
                f"project_timeline_duration: {pi.get('project_timeline_duration')} µs"
            )
            save = str(pi.get("proj_zip_save_path", "")).replace("/", "\\")
            if save and Path(save).resolve() != wfp_path.resolve():
                issues.append("proj_zip_save_path does not match .wfp location")

            medias = json.loads(zf.read("ProjectFolder/Medias/medias_info.json"))
            for item in medias.get("media_items", {}).values():
                url = str(item.get("download_url", "") or "")
                if url.startswith(("C:", "D:")):
                    local = Path(url.replace("/", "\\"))
                    if not local.is_file():
                        issues.append(f"MISSING media: {local}")

            timeline_id = pi.get("timeline_mediaId")
            if timeline_id:
                tl = json.loads(
                    zf.read(f"ProjectFolder/Medias/{timeline_id}/timeline.wesproj")
                )
                zero = 0
                for track in tl.get("timelineInfos", [{}])[0].get("trackInfos", []):
                    for clip in track.get("clipList") or []:
                        tl_b = int(clip.get("tlBegin") or 0)
                        tl_e = int(clip.get("tlEnd") or 0)
                        if tl_e <= tl_b and _VIDEO_FILE_RE.search(
                            str(clip.get("filename", "") or "")
                        ):
                            zero += 1
                if zero:
                    issues.append(f"Zero-length video clips on timeline: {zero}")
    except zipfile.BadZipFile:
        issues.append("File is not a valid zip archive (.wfp)")
    except json.JSONDecodeError as exc:
        issues.append(f"Invalid JSON inside project: {exc}")
    except KeyError as exc:
        issues.append(f"Unexpected project structure: {exc}")
    return issues


def log_export_audit(wfp_path: Path) -> None:
    """Print export audit lines to the console."""
    log_step("export", f"Audit {wfp_path.name}")
    notes = audit_export_wfp(wfp_path)
    for line in notes:
        if line.startswith("MISSING") or "Zero-length" in line:
            log_warn("export", line)
        else:
            log_info("export", line)
    if not any(n.startswith("MISSING") for n in notes):
        log_info("export", "All bundled media paths exist on disk")


def _validate_export_media_paths(wfp_path: Path, bundle: ExportMediaBundle) -> None:
    """Ensure every media path referenced in the export exists on disk."""
    missing: list[str] = []
    with zipfile.ZipFile(wfp_path) as zf:
        medias = json.loads(zf.read("ProjectFolder/Medias/medias_info.json"))
        for item in medias.get("media_items", {}).values():
            url = str(item.get("download_url", "") or "")
            if not url.startswith(("C:", "D:")):
                continue
            local = Path(url.replace("/", "\\"))
            if local.suffix.lower() in {".mp4", ".mov", ".m4a", ".mp3", ".jpg", ".jpeg", ".png"}:
                if not local.is_file():
                    missing.append(url)
    if missing:
        raise FileNotFoundError(
            "Export references media files Filmora cannot find:\n"
            + "\n".join(f"  - {p}" for p in missing[:8])
        )
    for label, path in (
        ("joined", bundle.joined),
        ("source", bundle.source),
        ("music", bundle.music),
    ):
        if path is not None and not path.is_file():
            missing.append(f"{label}: {path}")
    if missing:
        raise FileNotFoundError(
            "Export bundle is incomplete:\n" + "\n".join(f"  - {p}" for p in missing)
        )


def reference_media_dir() -> Path:
    return template_path().parent / REFERENCE_MEDIA_DIR_NAME


def _ensure_reference_media(ref_dir: Path) -> dict[str, Path]:
    """Local media for the bundled template (real bundle files or tiny placeholders)."""
    bundle = template_media_bundle()
    if bundle is not None:
        return bundle

    ref_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "highlight": ref_dir / "placeholder_highlight.mp4",
        "source": ref_dir / "placeholder_source.mp4",
        "music": ref_dir / "placeholder_music.m4a",
        "thumb": ref_dir / "placeholder_thumb.jpg",
    }
    duration = "0.2"
    if not paths["highlight"].is_file():
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c=black:s=1080x1920:d={duration}",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-t",
                duration,
                str(paths["highlight"]),
            ],
            check=True,
            capture_output=True,
        )
    if not paths["source"].is_file():
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c=black:s=1080x1920:d={duration}",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-t",
                duration,
                str(paths["source"]),
            ],
            check=True,
            capture_output=True,
        )
    if not paths["music"].is_file():
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=f=440:d={duration}",
                "-c:a",
                "aac",
                "-t",
                duration,
                str(paths["music"]),
            ],
            check=True,
            capture_output=True,
        )
    if not paths["thumb"].is_file():
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=gray:s=320x180",
                "-frames:v",
                "1",
                str(paths["thumb"]),
            ],
            check=True,
            capture_output=True,
        )
    return paths


def sanitize_bundled_template_wfp() -> Path:
    """
    Rewrite assets/filmora_templates/filmora_14_2_9.wfp so it only references
    checked-in placeholder media (no D:/Facebook_1 paths).
    """
    template = template_path()
    if not template.is_file():
        raise FileNotFoundError(f"Template missing: {template}")

    refs = _ensure_reference_media(reference_media_dir())

    extract_dir = TEMP / f"wfp_sanitize_{int(time.time())}"
    extract_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(template) as zf:
        zf.extractall(extract_dir)

    replacements: dict[str, str] = {}
    project_info_path = extract_dir / "ProjectFolder" / "project_info.json"
    medias_info_path = extract_dir / "ProjectFolder" / "Medias" / "medias_info.json"
    project_info = _read_json(project_info_path.read_bytes()) if project_info_path.is_file() else {}
    medias_info = _read_json(medias_info_path.read_bytes()) if medias_info_path.is_file() else {}
    layout = _load_template_layout(project_info, medias_info)

    id_targets: dict[str, Path] = {}
    if layout.source_video_id:
        id_targets[layout.source_video_id] = refs["source"]
    if layout.joined_video_id and layout.joined_video_id != layout.source_video_id:
        id_targets[layout.joined_video_id] = refs["highlight"]
    for audio_id in layout.audio_ids:
        id_targets[audio_id] = refs["music"]
    for image_id in layout.image_ids:
        id_targets[image_id] = refs["thumb"]

    if medias_info_path.is_file():
        for media_id, item in medias_info.get("media_items", {}).items():
            target = id_targets.get(media_id)
            if target is None or not target.is_file():
                continue
            duration_us = _us(_safe_duration(target, 0.2))
            _assign_media_item(
                item,
                new_path=target.resolve(),
                duration_us=duration_us,
                replacements=replacements,
            )
        medias_info_path.write_bytes(_write_json(medias_info))

    _patch_media_json_files(
        extract_dir,
        layout,
        video_path=refs["highlight"].resolve(),
        source_video_path=refs["source"].resolve(),
        audio_path=refs["music"].resolve(),
        cover_path=refs["thumb"].resolve(),
        replacements=replacements,
    )

    timeline_path = extract_dir / layout.timeline_rel_path
    if timeline_path.is_file():
        timeline_text = timeline_path.read_text(encoding="utf-8")
        timeline_text = _replace_paths_in_text(timeline_text, replacements)
        try:
            timeline_data = json.loads(timeline_text)
            layout = _layout_with_timeline_uuids(layout, timeline_data)
            vid_us = _us(_safe_duration(refs["highlight"], 0.2))
            src_us = _us(_safe_duration(refs["source"], 0.2))
            aud_us = _us(_safe_duration(refs["music"], 0.2))
            _patch_timeline_resources(
                timeline_data,
                source_path=refs["source"].resolve(),
                joined_path=refs["highlight"].resolve(),
                audio_path=refs["music"].resolve(),
                cover_path=refs["thumb"].resolve(),
                source_duration_us=src_us,
                joined_duration_us=vid_us,
                audio_duration_us=aud_us,
            )
            timeline_duration_us = max(vid_us, src_us, aud_us)
            timeline_text = json.dumps(timeline_data, ensure_ascii=False, separators=(",", ":"))
            timeline_text = _patch_timeline_wesproj(
                timeline_text,
                project=ProjectState(),
                layout=layout,
                source_path=refs["source"].resolve(),
                joined_path=refs["highlight"].resolve(),
                audio_path=refs["music"].resolve(),
                cover_path=refs["thumb"].resolve(),
                source_duration_us=src_us,
                joined_duration_us=vid_us,
                audio_duration_us=aud_us,
                timeline_duration_us=timeline_duration_us,
            )
        except json.JSONDecodeError:
            timeline_text = _rewrite_timeline_joined_filenames(
                timeline_text, refs["highlight"].resolve(), layout.path_markers
            )
            vid_us = _us(_safe_duration(refs["highlight"], 0.2))
            src_us = _us(_safe_duration(refs["source"], 0.2))
            aud_us = _us(_safe_duration(refs["music"], 0.2))
            timeline_text = _repair_timeline_inouts(
                timeline_text,
                project=ProjectState(),
                source_duration_us=src_us,
                joined_duration_us=vid_us,
                audio_duration_us=aud_us,
                timeline_duration_us=max(vid_us, aud_us),
            )
            timeline_text = _sanitize_timeline_filenames(timeline_text)
        timeline_path.write_text(timeline_text, encoding="utf-8")

    _apply_replacements_to_extract_dir(extract_dir, replacements)

    backup = template.with_suffix(".wfp.bak")
    if not backup.is_file():
        backup.write_bytes(template.read_bytes())

    with zipfile.ZipFile(template, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(extract_dir.rglob("*")):
            if file_path.is_file():
                arcname = file_path.relative_to(extract_dir).as_posix()
                zf.write(file_path, arcname)

    return template.resolve()
