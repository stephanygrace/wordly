from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from utils.console_log import log_info, log_step
from utils.ffmpeg_paths import ffmpeg_bin_dir
from utils.paths import ASSETS

ProgressCallback = Callable[[float, str], None]
ShouldCancel = Callable[[], bool]

AUDIO_EXTENSIONS = (".mp3", ".m4a", ".opus", ".webm", ".ogg", ".aac", ".wav")


def _looks_like_url(query: str) -> bool:
    q = query.strip().lower()
    return q.startswith("http://") or q.startswith("https://")


def _resolve_output_path(info: dict, out_dir: Path, ydl) -> Path:
    fp = info.get("filepath")
    if fp:
        path = Path(fp)
        if path.suffix.lower() in AUDIO_EXTENSIONS and path.is_file():
            return path.resolve()

    vid = str(info.get("id") or "")
    if vid:
        for ext in AUDIO_EXTENSIONS:
            matches = sorted(
                out_dir.glob(f"*{vid}*{ext}"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if matches:
                return matches[0].resolve()

    prepared = Path(ydl.prepare_filename(info))
    if prepared.is_file() and prepared.suffix.lower() in AUDIO_EXTENSIONS:
        return prepared.resolve()

    audio_files = sorted(
        (p for p in out_dir.iterdir() if p.suffix.lower() in AUDIO_EXTENSIONS),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if audio_files:
        return audio_files[0].resolve()

    raise FileNotFoundError("Instrumental download finished but audio file was not found.")


def _emit(
    progress_cb: Optional[ProgressCallback],
    ratio: float,
    msg: str,
) -> None:
    if progress_cb is not None:
        progress_cb(ratio, msg)


def download_instrumental(
    search_query: str,
    *,
    output_dir: Optional[Path] = None,
    progress_cb: Optional[ProgressCallback] = None,
    should_cancel: Optional[ShouldCancel] = None,
) -> Path:
    """Download instrumental audio via yt-dlp (YouTube search or direct URL)."""
    try:
        import yt_dlp
        from yt_dlp.utils import DownloadCancelled
    except ImportError as exc:
        raise RuntimeError("yt-dlp is not installed.") from exc

    out_dir = output_dir or (ASSETS / "music")
    out_dir.mkdir(parents=True, exist_ok=True)
    template = str(out_dir / "%(title).80s [%(id)s].%(ext)s")
    query = search_query.strip()
    is_url = _looks_like_url(query)
    if is_url:
        log_step("music", f"Downloading audio from URL: {query[:120]}")
    else:
        log_step("music", f"Searching YouTube for: {query}")

    def hook(d: dict) -> None:
        if should_cancel and should_cancel():
            raise DownloadCancelled()
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes") or 0
            ratio = min(1.0, downloaded / float(total)) if total else -1.0
            if total:
                _emit(
                    progress_cb,
                    ratio,
                    f"Downloading audio ({downloaded // 1024} KiB / {int(total) // 1024} KiB)",
                )
            else:
                _emit(progress_cb, ratio, "Downloading audio…")
        elif status == "finished":
            _emit(progress_cb, 0.98, "Audio download finished")

    ydl_opts: dict = {
        # Prefer YouTube's native m4a/opus audio — skip FFmpegExtractAudio entirely.
        "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
        "outtmpl": template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [hook],
        "retries": 5,
        "socket_timeout": 30,
    }

    if not is_url:
        ydl_opts["default_search"] = "ytsearch1"

    bin_dir = ffmpeg_bin_dir()
    if bin_dir:
        ydl_opts["ffmpeg_location"] = str(bin_dir)

    _emit(progress_cb, -1.0, "Resolving audio…" if is_url else "Searching YouTube…")

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(query, download=True)
        if info.get("_type") == "playlist":
            entries = info.get("entries") or []
            if not entries:
                raise RuntimeError("YouTube search returned no results.")
            info = entries[0]
        path = _resolve_output_path(info, out_dir, ydl)

    _emit(progress_cb, 1.0, f"Saved {path.name}")
    log_info("music", f"Instrumental ready: {path}")
    return path.resolve()
