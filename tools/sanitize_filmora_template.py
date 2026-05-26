#!/usr/bin/env python3
"""Replace machine-specific paths in the bundled Filmora template with local placeholders."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.filmora_14_wfp import reference_media_dir, sanitize_bundled_template_wfp


def main() -> int:
    out = sanitize_bundled_template_wfp()
    print(f"Sanitized template: {out}")
    print(f"Reference media: {reference_media_dir()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
