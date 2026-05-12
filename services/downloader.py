from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable, Optional

ShouldCancel = Callable[[], bool]

from utils.paths import DOWNLOADS


ProgressCallback = Callable[[float, str], None]


def _find_ffmpeg() -> Optional[str]:
    return shutil.which("ffmpeg")


def download_facebook_video(
    url: str,
    *,
    progress_cb: Optional[ProgressCallback] = None,
    output_dir: Optional[Path] = None,
    should_cancel: Optional[ShouldCancel] = None,
    cookies_file: Optional[Path] = None,
) -> Path:
    """
    Download a Facebook / Facebook Live video with yt-dlp.

    Returns path to the merged video file.
    """
    try:
        import yt_dlp
        from yt_dlp.utils import DownloadCancelled
    except ImportError as exc:  # pragma: no cover - env guard
        raise RuntimeError("yt-dlp is not installed. Install requirements.txt.") from exc

    out_dir = output_dir or DOWNLOADS
    out_dir.mkdir(parents=True, exist_ok=True)

    template = str(out_dir / "%(title).80s [%(id)s].%(ext)s")

    def hook(d: dict) -> None:
        if should_cancel is not None and should_cancel():
            raise DownloadCancelled()
        if progress_cb is None:
            return
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes") or 0
            if total:
                progress_cb(min(1.0, downloaded / float(total)), "Downloading…")
            else:
                progress_cb(-1.0, "Downloading…")
        elif status == "finished":
            progress_cb(1.0, "Post-processing…")

    ydl_opts: dict = {
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": template,
        "progress_hooks": [hook],
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }

    ffmpeg = _find_ffmpeg()
    if ffmpeg:
        ydl_opts["ffmpeg_location"] = str(Path(ffmpeg).parent)

    if cookies_file is not None:
        cf = cookies_file.expanduser()
        if cf.is_file():
            ydl_opts["cookiefile"] = str(cf.resolve())

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)

        path: Path | None = None
        fp = info.get("filepath")
        if fp:
            path = Path(fp)

        requested = info.get("requested_downloads")
        if path is None and requested:
            path = Path(requested[0]["filepath"])

        if path is None:
            path = Path(ydl.prepare_filename(info))

        if not path.exists():
            vid = str(info.get("id") or "")
            candidates = sorted(out_dir.glob(f"*{vid}*"), key=lambda p: p.stat().st_mtime, reverse=True)
            for candidate in candidates:
                if candidate.suffix.lower() in {".mp4", ".webm", ".mkv", ".m4v"}:
                    path = candidate
                    break

    if not path.exists():
        raise FileNotFoundError(f"Download finished but file not found: {path}")

    return path.resolve()
