from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable, Optional

ShouldCancel = Callable[[], bool]

from services.download_backend import aria2c_available, download_with_idm, find_idm_executable, idm_available
from utils.console_log import log_info
from utils.paths import DOWNLOADS


ProgressCallback = Callable[[float, str], None]


def _log_download(message: str) -> None:
    log_info("download", message)


def _progress_with_console(
    progress_cb: Optional[ProgressCallback],
    *,
    prefix: str = "",
) -> ProgressCallback:
    def emit(ratio: float, message: str) -> None:
        line = f"{prefix}{message}" if prefix else message
        _log_download(line)
        if progress_cb is not None:
            progress_cb(ratio, line)

    return emit


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


def _find_ffmpeg() -> Optional[str]:
    return shutil.which("ffmpeg")


def _is_facebook_page_url(url: str) -> bool:
    lower = url.lower()
    return any(host in lower for host in ("facebook.com", "fb.watch", "fb.com"))


def _sanitize_download_stem(name: str, *, fallback: str = "wordly-download") -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in " ._-()[]" else "_" for ch in name.strip())
    cleaned = " ".join(cleaned.split())
    return (cleaned[:80] or fallback).strip(" ._-")


def _format_score(fmt: dict) -> tuple[int, int, int, int, float]:
    has_audio = fmt.get("acodec") not in (None, "none")
    ext = (fmt.get("ext") or "").lower()
    protocol = (fmt.get("protocol") or "").lower()
    prefers_mp4 = ext == "mp4"
    prefers_hls = "m3u8" in protocol or ext == "m3u8"
    return (
        1 if has_audio else 0,
        1 if prefers_mp4 else 0,
        1 if prefers_hls else 0,
        int(fmt.get("height") or 0),
        float(fmt.get("tbr") or 0),
    )


def _pick_direct_format(info: dict) -> tuple[str, dict[str, str]]:
    """Choose a direct media URL IDM can fetch (not an HTML page)."""
    direct = info.get("url")
    if isinstance(direct, str) and direct.startswith(("http://", "https://")):
        headers = dict(info.get("http_headers") or {})
        return direct, headers

    requested = info.get("requested_formats") or []
    if len(requested) == 1:
        fmt = requested[0]
        url = fmt.get("url")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            headers = dict(fmt.get("http_headers") or info.get("http_headers") or {})
            return url, headers

    formats = [fmt for fmt in (info.get("formats") or []) if isinstance(fmt, dict)]
    ranked = sorted(
        (
            fmt
            for fmt in formats
            if isinstance(fmt.get("url"), str)
            and str(fmt["url"]).startswith(("http://", "https://"))
            and fmt.get("vcodec") not in (None, "none")
        ),
        key=_format_score,
        reverse=True,
    )
    if not ranked:
        raise RuntimeError("yt-dlp did not return a direct media stream URL for this page.")

    # Facebook live often exposes separate audio/video; prefer one combined stream.
    for fmt in ranked:
        if fmt.get("acodec") not in (None, "none"):
            return str(fmt["url"]), dict(fmt.get("http_headers") or info.get("http_headers") or {})

    best = ranked[0]
    return str(best["url"]), dict(best.get("http_headers") or info.get("http_headers") or {})


def _ytdlp_probe_opts(*, cookies_file: Optional[Path] = None) -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 10,
        "format": (
            "best*[acodec!=none][vcodec!=none]/"
            "best[ext=mp4][acodec!=none][vcodec!=none]/"
            "best[acodec!=none][vcodec!=none]/"
            "best"
        ),
    }
    if cookies_file is not None:
        cf = cookies_file.expanduser()
        if cf.is_file():
            opts["cookiefile"] = str(cf.resolve())
    ffmpeg = _find_ffmpeg()
    if ffmpeg:
        opts["ffmpeg_location"] = str(Path(ffmpeg).parent)
    return opts


def resolve_facebook_direct_url(
    page_url: str,
    *,
    cookies_file: Optional[Path] = None,
    progress_cb: Optional[ProgressCallback] = None,
) -> tuple[str, dict[str, str], str]:
    """
    Resolve a Facebook page/live URL to a direct media stream URL via yt-dlp.

    IDM cannot download Facebook HTML pages; it needs the signed CDN/HLS URL.
    """
    try:
        import yt_dlp
    except ImportError as exc:  # pragma: no cover - env guard
        raise RuntimeError("yt-dlp is not installed. Install requirements.txt.") from exc

    if progress_cb is not None:
        progress_cb(-1.0, "Resolving Facebook stream URL with yt-dlp…")
    _log_download("Resolving Facebook page to direct media URL with yt-dlp…")

    with yt_dlp.YoutubeDL(_ytdlp_probe_opts(cookies_file=cookies_file)) as ydl:
        info = ydl.extract_info(page_url, download=False)

    if not isinstance(info, dict):
        raise RuntimeError("Could not read video metadata from Facebook.")

    media_url, headers = _pick_direct_format(info)
    title = _sanitize_download_stem(str(info.get("title") or "wordly-download"))
    _log_download(f"Resolved direct media URL for IDM ({title})")
    return media_url, headers, title


def download_facebook_video(
    url: str,
    *,
    progress_cb: Optional[ProgressCallback] = None,
    output_dir: Optional[Path] = None,
    should_cancel: Optional[ShouldCancel] = None,
    cookies_file: Optional[Path] = None,
    use_idm: bool = False,
    concurrent_fragments: int = 16,
) -> Path:
    """
    Download a Facebook / Facebook Live video with yt-dlp.

  When ``use_idm`` is True on Windows, the URL is handed to Internet Download Manager
  and Wordly waits for the finished file in ``output_dir``.

    Returns path to the merged video file.
    """
    out_dir = output_dir or DOWNLOADS
    out_dir.mkdir(parents=True, exist_ok=True)

    if use_idm:
        idm_exe = find_idm_executable()
        if idm_exe is None:
            raise RuntimeError("Internet Download Manager was not found on this system.")
        _log_download(f"Downloading via Internet Download Manager (IDM): {idm_exe}")
        emit = _progress_with_console(progress_cb, prefix="[IDM] ")
        media_url = url
        suggested_name: str | None = None
        if _is_facebook_page_url(url):
            media_url, _headers, title = resolve_facebook_direct_url(
                url,
                cookies_file=cookies_file,
                progress_cb=emit,
            )
            suggested_name = f"{title}.mp4"
            emit(-1.0, f"Handing direct stream to IDM — {title}")
        return download_with_idm(
            media_url,
            out_dir,
            progress_cb=emit,
            should_cancel=should_cancel,
            suggested_filename=suggested_name,
        )

    try:
        import yt_dlp
        from yt_dlp.utils import DownloadCancelled
    except ImportError as exc:  # pragma: no cover - env guard
        raise RuntimeError("yt-dlp is not installed. Install requirements.txt.") from exc

    template = str(out_dir / "%(title).80s [%(id)s].%(ext)s")

    def _emit(ratio: float, msg: str) -> None:
        _log_download(msg)
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

    ydl_opts: dict = {
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": template,
        "progress_hooks": [hook],
        "postprocessor_hooks": [post_hook],
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": max(1, int(concurrent_fragments)),
        "http_chunk_size": 10 * 1024 * 1024,
    }

    if aria2c_available():
        ydl_opts["external_downloader"] = "aria2c"
        ydl_opts["external_downloader_args"] = {
            "aria2c": ["-x", "16", "-s", "16", "-k", "1M", "--file-allocation=none"]
        }
        _emit(-1.0, "Using aria2c for faster parallel download…")
    else:
        _emit(-1.0, f"Using yt-dlp with {ydl_opts['concurrent_fragment_downloads']} concurrent fragments…")

    ffmpeg = _find_ffmpeg()
    if ffmpeg:
        ydl_opts["ffmpeg_location"] = str(Path(ffmpeg).parent)

    if cookies_file is not None:
        cf = cookies_file.expanduser()
        if cf.is_file():
            ydl_opts["cookiefile"] = str(cf.resolve())

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        _emit(-1.0, "Fetching video info…")
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
