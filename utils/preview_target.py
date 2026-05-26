from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PreviewDimensions:
    width: int
    height: int


def preview_target_dimensions(frame: PreviewDimensions, host: PreviewDimensions) -> PreviewDimensions:
    """Pick a non-zero size for frame scaling (layout may not have run yet)."""
    for candidate in (frame, host):
        if candidate.width >= 8 and candidate.height >= 8:
            return candidate
    return PreviewDimensions(640, 360)
