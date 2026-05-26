#!/usr/bin/env python3
"""Find leftover template path references in a .wfp."""
import sys
import zipfile
from pathlib import Path

NEEDLES = (
    "Facebook_1",
    "Facebook.mp4",
    "Any Video Converter",
    "Format Convert",
    "D:/",
    "D:\\",
)


def scan(path: Path) -> None:
    print(f"=== {path} ===")
    with zipfile.ZipFile(path) as zf:
        for name in sorted(zf.namelist()):
            try:
                text = zf.read(name).decode("utf-8")
            except (UnicodeDecodeError, KeyError):
                continue
            hits = [n for n in NEEDLES if n in text]
            if hits:
                print(f"  {name}: {hits}")
                for n in NEEDLES:
                    if n in text:
                        i = text.index(n)
                        snippet = text[max(0, i - 60) : i + len(n) + 60]
                        print(f"    ...{snippet!r}...")


if __name__ == "__main__":
    paths = sys.argv[1:]
    if not paths:
        print("Usage: python tools/scan_wfp_refs.py <file.wfp> [...]")
        return
    for p in paths:
        path = Path(p)
        if path.is_file():
            scan(path.resolve())
