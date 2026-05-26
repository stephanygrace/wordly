#!/usr/bin/env python3
"""Verify an exported .wfp opens cleanly (valid timeline JSON, no bad paths)."""
import json
import sys
import zipfile
from pathlib import Path

path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("exports/wordly-project-windows.wfp")
TIMELINE = "ProjectFolder/Medias/{CE578FD0-98CF-4080-A85C-D05F1DCA0A93}/timeline.wesproj"
bad_clip = '"inPoint":48137422667,"outPoint":48297916334'

with zipfile.ZipFile(path) as zf:
    tl = zf.read(TIMELINE).decode("utf-8")

try:
    json.loads(tl)
    print("timeline JSON: OK")
except json.JSONDecodeError as exc:
    print("timeline JSON: BROKEN", exc)

print("backslashes in paths:", "\\Users" in tl or "file:/file:" in tl)
print("stale template clip trim:", bad_clip in tl)
