from __future__ import annotations

from datetime import datetime


def _stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log_info(component: str, message: str) -> None:
    print(f"[{_stamp()}] [Wordly:{component}] {message}", flush=True)


def log_step(component: str, message: str) -> None:
    print(f"[{_stamp()}] [Wordly:{component}] → {message}", flush=True)


def log_progress(component: str, message: str, *, ratio: float | None = None) -> None:
    if ratio is not None and ratio >= 0:
        pct = int(min(100, max(0, round(ratio * 100))))
        print(f"[{_stamp()}] [Wordly:{component}] {pct}% — {message}", flush=True)
    else:
        print(f"[{_stamp()}] [Wordly:{component}] {message}", flush=True)


def log_warn(component: str, message: str) -> None:
    print(f"[{_stamp()}] [Wordly:{component}] WARN: {message}", flush=True)


def log_error(component: str, message: str) -> None:
    print(f"[{_stamp()}] [Wordly:{component}] ERROR: {message}", flush=True)
