from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AudioMixSpec:
    sermon_volume: float  # 0.0–1.0 linear
    piano_volume: float
    piano_fade_in: bool
    piano_fade_out: bool
    clip_duration_seconds: float


def sermon_volume_from_ui(percent: int) -> float:
    return max(0.0, min(1.0, percent / 100.0))


def piano_volume_from_ui(percent: int) -> float:
    return max(0.0, min(1.0, percent / 100.0))


def build_piano_filter(
    spec: AudioMixSpec,
) -> str:
    """
    Build FFmpeg filter chain for piano track (index 1:a) before mixing.

    Output label [piano].
    """
    vol = spec.piano_volume
    d = spec.clip_duration_seconds
    chain: list[str] = []

    # Loop short music to cover clip length
    chain.append("aloop=loop=-1:size=2e+09")

    if spec.piano_fade_in and d > 0:
        fi = min(2.0, d * 0.1)
        chain.append(f"afade=t=in:st=0:d={fi:.3f}")

    if spec.piano_fade_out and d > 0:
        fo = min(3.0, d * 0.15)
        st = max(0.0, d - fo)
        chain.append(f"afade=t=out:st={st:.3f}:d={fo:.3f}")

    chain.append(f"volume={vol:.4f}")
    chain.append(f"atrim=0:{d:.3f}")
    chain.append("asetpts=PTS-STARTPTS")

    inner = ",".join(chain)
    return f"[1:a]{inner}[piano]"


def build_amix_filter() -> str:
    return "[sermon][piano]amix=inputs=2:duration=first:dropout_transition=2[aout]"
