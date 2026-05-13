#!/usr/bin/env python3
"""Inspect a Filmora .wfp archive (rename to .zip or pass .wfp directly)."""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path


def inspect_wfp(path: Path) -> None:
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        print(f"Archive: {path}")
        print(f"Entries: {len(names)}")
        for name in sorted(names):
            info = zf.getinfo(name)
            print(f"  {name} ({info.file_size} bytes)")
            if name.lower().endswith(".json"):
                try:
                    data = json.loads(zf.read(name).decode("utf-8"))
                except Exception as exc:  # noqa: BLE001
                    print(f"    (not valid JSON: {exc})")
                    continue
                if isinstance(data, dict):
                    print(f"    keys: {', '.join(sorted(data.keys())[:20])}")
                elif isinstance(data, list):
                    print(f"    list[{len(data)}]")


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect Filmora .wfp project archives.")
    parser.add_argument("wfp", type=Path, help="Path to .wfp file")
    args = parser.parse_args()
    inspect_wfp(args.wfp.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
