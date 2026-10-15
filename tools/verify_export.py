#!/usr/bin/env python3
"""Verify a Wordly Resolve export bundle (FCPXML + media + manifest)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
import xml.etree.ElementTree as ET


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python tools/verify_export.py exports/<name>/<name>.fcpxml", file=sys.stderr)
        return 2

    timeline = Path(sys.argv[1]).resolve()
    if not timeline.is_file():
        print(f"Missing timeline: {timeline}", file=sys.stderr)
        return 1

    bundle = timeline.parent
    media = bundle / "media"
    manifest_path = bundle / "wordly_manifest.json"
    issues: list[str] = []

    try:
        root = ET.parse(timeline).getroot()
        if "fcpxml" not in root.tag.lower():
            issues.append(f"Root tag is not fcpxml: {root.tag}")
    except ET.ParseError as exc:
        issues.append(f"FCPXML parse error: {exc}")

    if not media.is_dir():
        issues.append("media/ folder missing")
    else:
        clips = sorted(media.glob("Clip*.mp4")) + sorted(media.glob("Clip*.mov"))
        if not clips:
            issues.append("No Clip00N media files found")
        instrumentals = list(media.glob("instrumental.*"))
        if not instrumentals:
            issues.append("instrumental.* missing under media/")

    if not (bundle / "verse.srt").is_file():
        issues.append("verse.srt missing")
    if not (bundle / "OPEN_IN_RESOLVE.txt").is_file():
        issues.append("OPEN_IN_RESOLVE.txt missing")

    if manifest_path.is_file():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not data.get("wordly_export"):
                issues.append("manifest missing wordly_export=true")
            if data.get("nle") != "davinci_resolve_free":
                issues.append("manifest nle is not davinci_resolve_free")
        except json.JSONDecodeError as exc:
            issues.append(f"manifest JSON broken: {exc}")
    else:
        issues.append("wordly_manifest.json missing")

    if issues:
        print("INVALID")
        for issue in issues:
            print(f"  - {issue}")
        return 1

    print("OK")
    print(f"  timeline: {timeline}")
    print(f"  media:    {media}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
