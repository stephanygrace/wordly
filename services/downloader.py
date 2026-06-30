from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

ShouldCancel = Callable[[], bool]

from utils.ffmpeg_paths import ffmpeg_bin_dir, find_aria2c
from utils.paths import DOWNLOADS


ProgressCallback = Callable[[float, str], None]


def _fmt_bytes(num: float | int) -> str:
    n = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024.0 or unit == "TiB":
            if unit == "B":
                return f"{int(n)} B"
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TiB"


def _fmt_rate(bps: float | int | None) -> str | None:
    if bps is None:
        return None
    rate = float(bps)
    if rate <= 0:
        return None
    return f"{_fmt_bytes(rate)}/s"


def _fmt_eta(seconds: float | int | None) -> str | None:
    if seconds is None:
        return None
    total = int(max(0, seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


def _download_ratio(d: dict) -> float:
    """Best-effort 0..1 ratio from a yt-dlp progress hook payload; -1 if unknown."""
    total = d.get("total_bytes") or d.get("total_bytes_estimate")
    downloaded = d.get("downloaded_bytes") or 0
    if total:
        return min(1.0, downloaded / float(total))
    frag_count = d.get("fragment_count")
    if frag_count:
        frag_idx = int(d.get("fragment_index") or 0)
        return min(1.0, max(0.0, frag_idx / float(frag_count)))
    return -1.0


def _download_status_message(d: dict, ratio: float) -> str:
    """Human-readable line for UI and console (yt-dlp progress hook payload)."""
    extras: list[str] = []
    rate_s = _fmt_rate(d.get("speed"))
    if rate_s:
        extras.append(rate_s)
    eta_s = _fmt_eta(d.get("eta"))
    if eta_s:
        extras.append(f"ETA {eta_s}")
    tail = f" · {' · '.join(extras)}" if extras else ""

    total = d.get("total_bytes") or d.get("total_bytes_estimate")
    downloaded = d.get("downloaded_bytes") or 0
    frag_count = d.get("fragment_count")
    frag_idx = d.get("fragment_index")
    if total:
        pct = int(min(100, max(0, round(ratio * 100))))
        base = f"Downloading — {pct}% ({_fmt_bytes(downloaded)} / {_fmt_bytes(float(total))})"
        return base + tail
    if frag_count and frag_idx is not None:
        pct = int(min(100, max(0, round(ratio * 100))))
        base = f"Downloading — {pct}% (fragment {frag_idx}/{frag_count}, {_fmt_bytes(downloaded)})"
        return base + tail
    if downloaded:
        base = f"Downloading — {_fmt_bytes(downloaded)} (total size unknown)"
        return base + tail
    return "Downloading — starting…" + tail


def _progress_from_ytdlp_hook(d: dict) -> tuple[float, str] | None:
    status = d.get("status")
    if status == "downloading":
        ratio = _download_ratio(d)
        msg_ratio = ratio if ratio >= 0 else 0.0
        return ratio, _download_status_message(d, msg_ratio)
    if status == "finished":
        filename = d.get("filename") or d.get("tmpfilename") or ""
        label = Path(filename).name if filename else "stream"
        return -1.0, f"Finished {label} — continuing…"
    return None


def _progress_from_postprocessor_hook(d: dict) -> tuple[float, str] | None:
    status = d.get("status")
    pp = str(d.get("postprocessor") or "post-processor")
    if status == "started":
        return -1.0, f"Post-processing — {pp}…"
    if status == "processing":
        return -1.0, f"Post-processing — {pp}…"
    if status == "finished":
        return 0.99, "Post-processing — finalizing…"
    return None


def download_facebook_video(
    url: str,
    *,
    progress_cb: Optional[ProgressCallback] = None,
    output_dir: Optional[Path] = None,
    should_cancel: Optional[ShouldCancel] = None,
    cookies_file: Optional[Path] = None,
    concurrent_fragments: int = 16,
) -> Path:
    """Download a Facebook / Facebook Live video with yt-dlp.

    Uses yt-dlp's built-in HTTP downloader with parallel fragments so
    progress hooks fire continuously during the download.

    Returns path to the merged video file.
    """
    out_dir = output_dir or DOWNLOADS
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        import yt_dlp
        from yt_dlp.utils import DownloadCancelled
    except ImportError as exc:  # pragma: no cover - env guard
        raise RuntimeError("yt-dlp is not installed. Install requirements.txt.") from exc

    template = str(out_dir / "%(title).80s [%(id)s].%(ext)s")

    def _emit(ratio: float, msg: str) -> None:
        if progress_cb is not None:
            progress_cb(ratio, msg)

    def hook(d: dict) -> None:
        if should_cancel is not None and should_cancel():
            raise DownloadCancelled()
        update = _progress_from_ytdlp_hook(d)
        if update is not None:
            _emit(*update)

    def post_hook(d: dict) -> None:
        if should_cancel is not None and should_cancel():
            raise DownloadCancelled()
        update = _progress_from_postprocessor_hook(d)
        if update is not None:
            _emit(*update)

    _emit(-1.0, "Preparing download…")

    aria2c_path = find_aria2c()

    ydl_opts: dict = {
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": template,
        "progress_hooks": [hook],
        "postprocessor_hooks": [post_hook],
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": max(1, int(concurrent_fragments)),
        "http_chunk_size": 10 * 1024 * 1024,
        # Skip re-downloading if the merged output already exists.
        "nooverwrites": True,
        # Suppress side-car files that waste I/O.
        "writethumbnail": False,
        "writeinfojson": False,
        "writedescription": False,
        "writesubtitles": False,
        "writeautomaticsub": False,
    }

    if aria2c_path:
        # Use aria2c for plain HTTP/S streams; yt-dlp still handles DASH/HLS
        # fragment downloads itself via concurrent_fragment_downloads above.
        ydl_opts["external_downloader"] = {"default": aria2c_path}
        ydl_opts["external_downloader_args"] = {
            "aria2c": [
                "-x16",
                "-s16",
                "--min-split-size=1M",
                "--console-log-level=warn",
                "--summary-interval=0",
            ]
        }
        _emit(-1.0, f"Using aria2c + yt-dlp with {ydl_opts['concurrent_fragment_downloads']} concurrent fragments…")
    else:
        _emit(-1.0, f"Using yt-dlp with {ydl_opts['concurrent_fragment_downloads']} concurrent fragments…")

    bin_dir = ffmpeg_bin_dir()
    if bin_dir:
        ydl_opts["ffmpeg_location"] = str(bin_dir)

    if cookies_file is not None:
        cf = cookies_file.expanduser()
        if cf.is_file():
            ydl_opts["cookiefile"] = str(cf.resolve())

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        _emit(-1.0, "Fetching video info from Facebook…")
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
