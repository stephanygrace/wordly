from __future__ import annotations

import os
import pty
import re
import select
import subprocess
from pathlib import Path
from typing import Callable, Optional

ShouldCancel = Callable[[], bool]

from utils.ffmpeg_paths import ffmpeg_bin_dir
from utils.paths import DOWNLOADS
from utils.ytdlp_paths import packaged_yt_dlp_version, preferred_yt_dlp_executable, yt_dlp_backend_label


ProgressCallback = Callable[[float, str], None]

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_ANSI_OSC_RE = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)")

_DOWNLOAD_LINE_RE = re.compile(
    r"^\[download\]\s+"
    r"(?:"
    r"(?P<pct>[\d.]+)%"
    r"(?:\s+of\s+(?:~\s*)?(?P<total>[\d.]+\s*\w+))?"
    r"(?:\s+at\s+(?P<speed>[\d.]+\s*\w+/s))?"
    r"(?:\s+ETA\s+(?P<eta>\S+))?"
    r"|"
    r"(?P<only_bytes>[\d.]+\s*\w+)\s+at\s+(?P<only_speed>[\d.]+\s*\w+/s)"
    r")"
)


def _clean_ytdlp_line(text: str) -> str:
    text = _ANSI_OSC_RE.sub("", text)
    text = _ANSI_ESCAPE_RE.sub("", text)
    return text.strip()


def _feed_subprocess_buffer(buffer: str, on_line: Callable[[str], None]) -> str:
    """Split on carriage returns/newlines and emit each progress segment."""
    start = 0
    for i, ch in enumerate(buffer):
        if ch not in "\r\n":
            continue
        segment = _clean_ytdlp_line(buffer[start:i])
        if segment:
            on_line(segment)
        start = i + 1
    return buffer[start:]


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


def _progress_from_download_line(line: str) -> tuple[float, str] | None:
    stripped = _clean_ytdlp_line(line)
    if not stripped.startswith("[download]"):
        return None
    if "has already been downloaded" in stripped.lower():
        return 1.0, "Using existing download"
    if "destination:" in stripped.lower():
        dest = stripped.split(":", 1)[-1].strip()
        return -1.0, f"Downloading — {dest}"

    match = _DOWNLOAD_LINE_RE.match(stripped)
    if not match:
        return None

    extras: list[str] = []
    speed = match.group("speed") or match.group("only_speed")
    if speed:
        extras.append(speed.strip())
    eta = match.group("eta")
    if eta:
        extras.append(f"ETA {eta}")
    tail = f" · {' · '.join(extras)}" if extras else ""

    pct_text = match.group("pct")
    total = match.group("total")
    only_bytes = match.group("only_bytes")
    if pct_text is not None:
        ratio = min(1.0, max(0.0, float(pct_text) / 100.0))
        pct = int(round(ratio * 100))
        if total:
            base = f"Downloading — {pct}% ({total.strip()})"
        else:
            base = f"Downloading — {pct}%"
        return ratio, base + tail
    if only_bytes:
        return -1.0, f"Downloading — {only_bytes.strip()} (total size unknown)" + tail
    return None


def _progress_from_log_line(line: str) -> tuple[float, str] | None:
    stripped = _clean_ytdlp_line(line)
    if not stripped:
        return None

    if stripped.startswith("[download]"):
        return _progress_from_download_line(stripped)

    for tag, label in (
        ("[facebook]", "Facebook"),
        ("[info]", "Info"),
        ("[ExtractAudio]", "Audio"),
        ("[Merger]", "Merge"),
    ):
        if stripped.startswith(tag):
            msg = stripped[len(tag) :].strip()
            if msg:
                return -1.0, f"{label} — {msg}"
    return None


def _prefer_system_yt_dlp() -> tuple[str | None, str]:
    """Pick the yt-dlp backend without spawning --version on every download."""
    exe = preferred_yt_dlp_executable()
    if exe:
        return exe, yt_dlp_backend_label()
    packaged = packaged_yt_dlp_version()
    if packaged is not None:
        version_label = ".".join(str(part) for part in packaged)
        return None, f"venv yt-dlp {version_label}"
    return None, "venv yt-dlp"


def _resolve_downloaded_path(
    info: dict,
    ydl: object,
    out_dir: Path,
) -> Path:
    path: Path | None = None
    fp = info.get("filepath")
    if fp:
        path = Path(fp)

    requested = info.get("requested_downloads")
    if path is None and requested:
        path = Path(requested[0]["filepath"])

    if path is None:
        path = Path(ydl.prepare_filename(info))  # type: ignore[attr-defined]

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


def _run_subprocess_with_lines(
    cmd: list[str],
    *,
    should_cancel: Optional[ShouldCancel],
    on_line: Callable[[str], None],
) -> int:
    """Run *cmd* and invoke *on_line* for each merged stdout/stderr line."""
    if os.name == "nt":
        return _run_subprocess_with_lines_pipe(cmd, should_cancel=should_cancel, on_line=on_line)

    master_fd: int | None = None
    proc: subprocess.Popen[bytes] | None = None

    try:
        master_fd, slave_fd = pty.openpty()
        proc = subprocess.Popen(
            cmd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
        )
        os.close(slave_fd)

        buffer = ""
        while True:
            if should_cancel is not None and should_cancel():
                proc.terminate()
                try:
                    from yt_dlp.utils import DownloadCancelled
                except ImportError as exc:
                    raise RuntimeError("Download cancelled.") from exc
                raise DownloadCancelled()

            ready, _, _ = select.select([master_fd], [], [], 0.2)
            if master_fd in ready:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    if proc.poll() is not None:
                        break
                    continue
                buffer += chunk.decode("utf-8", errors="replace")
                buffer = _feed_subprocess_buffer(buffer, on_line)
            elif proc.poll() is not None:
                break

        if buffer.strip():
            buffer = _feed_subprocess_buffer(buffer + "\n", on_line)

        assert proc is not None
        return proc.wait()
    finally:
        if master_fd is not None:
            try:
                os.close(master_fd)
            except OSError:
                pass
        if proc is not None and proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)


def _run_subprocess_with_lines_pipe(
    cmd: list[str],
    *,
    should_cancel: Optional[ShouldCancel],
    on_line: Callable[[str], None],
) -> int:
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    try:
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            if should_cancel is not None and should_cancel():
                proc.terminate()
                try:
                    from yt_dlp.utils import DownloadCancelled
                except ImportError as exc:
                    raise RuntimeError("Download cancelled.") from exc
                raise DownloadCancelled()
            line = raw_line.rstrip("\r\n")
            if line:
                on_line(line)
    finally:
        return proc.wait()


def _download_with_executable(
    exe: str,
    url: str,
    *,
    template: str,
    progress_cb: Optional[ProgressCallback],
    should_cancel: Optional[ShouldCancel],
    cookies_file: Optional[Path],
) -> Path:
    def _emit(ratio: float, msg: str) -> None:
        if progress_cb is not None:
            progress_cb(ratio, msg)

    cmd: list[str] = [
        exe,
        "-f",
        "bv*+ba/b",
        "-o",
        template,
        "--no-playlist",
        "--no-warnings",
        "--color",
        "no",
        "--newline",
        "--print",
        "after_move:filepath",
    ]

    bin_dir = ffmpeg_bin_dir()
    if bin_dir:
        cmd.extend(["--ffmpeg-location", str(bin_dir)])

    if cookies_file is not None:
        cf = cookies_file.expanduser()
        if cf.is_file():
            cmd.extend(["--cookies", str(cf.resolve())])

    cmd.append(url)

    lines: list[str] = []
    output_path: Path | None = None

    def _on_line(line: str) -> None:
        lines.append(line)
        update = _progress_from_log_line(line)
        if update is not None:
            _emit(*update)
            return

        if line.startswith("[") and "]" in line:
            bracket = line[1 : line.index("]")]
            if bracket in {"ffmpeg", "FixupM3u8", "FixupM4a"}:
                _emit(-1.0, f"Post-processing — {bracket}…")

        candidate = Path(line.strip())
        if candidate.is_file():
            nonlocal output_path
            output_path = candidate

    return_code = _run_subprocess_with_lines(cmd, should_cancel=should_cancel, on_line=_on_line)

    if return_code != 0:
        tail = "\n".join(lines[-8:])
        raise RuntimeError(f"yt-dlp failed with exit code {return_code}\n{tail}")

    if output_path is None or not output_path.exists():
        raise FileNotFoundError("Download finished but output file was not reported by yt-dlp")

    return output_path.resolve()


def _download_with_python_api(
    url: str,
    *,
    template: str,
    out_dir: Path,
    progress_cb: Optional[ProgressCallback],
    should_cancel: Optional[ShouldCancel],
    cookies_file: Optional[Path],
) -> Path:
    try:
        import yt_dlp
        from yt_dlp.utils import DownloadCancelled
    except ImportError as exc:  # pragma: no cover - env guard
        raise RuntimeError("yt-dlp is not installed. Install requirements.txt.") from exc

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

    ydl_opts: dict = {
        "format": "bv*+ba/b",
        "outtmpl": template,
        "progress_hooks": [hook],
        "postprocessor_hooks": [post_hook],
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "nooverwrites": True,
        "writethumbnail": False,
        "writeinfojson": False,
        "writedescription": False,
        "writesubtitles": False,
        "writeautomaticsub": False,
    }

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
        return _resolve_downloaded_path(info, ydl, out_dir)


def download_backend_description() -> str:
    """Short label for the wizard UI."""
    return yt_dlp_backend_label()


def download_facebook_video(
    url: str,
    *,
    progress_cb: Optional[ProgressCallback] = None,
    output_dir: Optional[Path] = None,
    should_cancel: Optional[ShouldCancel] = None,
    cookies_file: Optional[Path] = None,
    concurrent_fragments: int = 16,  # kept for API compatibility; ignored
) -> Path:
    """Download a Facebook / Facebook Live video with yt-dlp.

    Prefers the user's system yt-dlp binary (e.g. ~/bin/yt-dlp from fbdl) when it
    is newer than the packaged module, using the same minimal flags as a plain
    terminal download.

    Returns path to the merged video file.
    """
    del concurrent_fragments  # fbdl uses yt-dlp defaults; extra tuning slowed FB live

    out_dir = output_dir or DOWNLOADS
    out_dir.mkdir(parents=True, exist_ok=True)

    def _emit(ratio: float, msg: str) -> None:
        if progress_cb is not None:
            progress_cb(ratio, msg)

    template = str(out_dir / "%(title).80s [%(id)s].%(ext)s")
    exe, backend = _prefer_system_yt_dlp()

    _emit(-1.0, "Preparing download…")
    _emit(-1.0, f"Using {backend}…")

    if exe:
        _emit(-1.0, "Fetching video info from Facebook…")
        return _download_with_executable(
            exe,
            url,
            template=template,
            progress_cb=progress_cb,
            should_cancel=should_cancel,
            cookies_file=cookies_file,
        )

    return _download_with_python_api(
        url,
        template=template,
        out_dir=out_dir,
        progress_cb=progress_cb,
        should_cancel=should_cancel,
        cookies_file=cookies_file,
    )
