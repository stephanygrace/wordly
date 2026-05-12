"""Optional OpenAI Whisper CLI → SRT (user installs ``pip install openai-whisper``)."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from utils.paths import TEMP

CancelCheck = Callable[[], bool]
ProgressCallback = Callable[[float, str], None]


def whisper_cli_path() -> str | None:
    return shutil.which("whisper")


def transcribe_media_to_srt(
    media_path: Path,
    *,
    model: str = "tiny",
    language: str | None = None,
    should_cancel: Optional[CancelCheck] = None,
    progress_cb: Optional[ProgressCallback] = None,
) -> Path:
    """
    Run ``whisper`` on ``media_path`` and return the path to the generated ``.srt``.

    Copies the result next to ``media_path`` as ``<stem>_whisper.srt`` so it survives
    temp cleanup. Requires the ``whisper`` command on PATH and FFmpeg for decoding.
    """
    exe = whisper_cli_path()
    if not exe:
        raise RuntimeError(
            "The Whisper CLI was not found on PATH. Install with:\n"
            "  pip install openai-whisper\n"
            "Then restart your terminal so `whisper` is available."
        )
    if not media_path.is_file():
        raise FileNotFoundError(str(media_path))

    TEMP.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="whisper_", dir=TEMP))
    try:
        cmd = [
            exe,
            str(media_path),
            "--output_dir",
            str(work),
            "--output_format",
            "srt",
            "--model",
            model,
        ]
        if language and language.strip():
            cmd.extend(["--language", language.strip()])

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        assert proc.stderr is not None

        def drain_stderr() -> None:
            try:
                for line in proc.stderr:
                    if progress_cb and line.strip():
                        progress_cb(-1.0, line.strip()[:120])
            except OSError:
                pass

        th = threading.Thread(target=drain_stderr, daemon=True)
        th.start()

        try:
            while True:
                if should_cancel and should_cancel():
                    proc.kill()
                    try:
                        proc.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        pass
                    raise RuntimeError("Cancelled")
                code = proc.poll()
                if code is not None:
                    break
                time.sleep(0.2)
            proc.wait(timeout=5)
        finally:
            try:
                proc.stderr.close()
            except OSError:
                pass
            th.join(timeout=2.0)

        if proc.returncode != 0:
            raise RuntimeError(f"whisper exited with code {proc.returncode}")

        produced = work / f"{media_path.stem}.srt"
        if not produced.is_file():
            raise RuntimeError(f"Whisper did not write {produced.name} under the temp output folder.")

        final = media_path.parent / f"{media_path.stem}_whisper.srt"
        shutil.copy2(produced, final)
        return final.resolve()
    finally:
        shutil.rmtree(work, ignore_errors=True)
