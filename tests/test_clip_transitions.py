from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from models.project import ClipSegment
from services import multi_clip
from services.reel_layout import background_join_filter, join_backgrounds
from services.resolve_export import (
    AUDIO_CROSSFADE_UID,
    CROSS_DISSOLVE_UID,
    _build_fcpxml,
    _seconds_to_fcpxml,
    _write_captions_srt,
)
from services.transcribe import Caption


class TestPaddedCuts(unittest.TestCase):
    def test_clip_is_cut_with_allowance_and_hidden_fade_frames(self) -> None:
        cmds: list[list[str]] = []
        with TemporaryDirectory() as tmp, patch.object(
            multi_clip, "_run_ffmpeg", side_effect=lambda cmd, **_k: cmds.append(cmd)
        ), patch.object(multi_clip, "_prefer_reencode_for_editing", return_value=False), patch.object(
            multi_clip, "_output_looks_valid", return_value=True
        ):
            multi_clip.export_segment_clip(Path("sermon.mp4"), ClipSegment("00:10:00", "00:10:30"), Path(tmp) / "c.mp4")
            multi_clip.export_segment_clip(Path("sermon.mp4"), ClipSegment("00:00:01", "00:00:20"), Path(tmp) / "d.mp4")
        first, second = cmds
        self.assertEqual((first[first.index("-ss") + 1], first[first.index("-to") + 1]), ("598.500", "631.500"))
        self.assertEqual(second[second.index("-ss") + 1], "0.000")  # never before the video starts


class TestDissolvesOnTimeline(unittest.TestCase):
    def test_one_second_dissolve_centred_on_each_cut(self) -> None:
        clips = [Path("/tmp/Clip001.mp4"), Path("/tmp/Clip002.mp4"), Path("/tmp/Clip003.mp4")]
        with patch("services.resolve_export._safe_duration", side_effect=[13.0, 11.0, 9.0]):
            root = _build_fcpxml(
                project_name="p",
                clip_paths=clips,
                music=None,
                timeline_duration_s=30.0,
                clip_captions=[[Caption(0.2, 0.4, "hidden"), Caption(0.3, 2.0, "edge cut")], [], []],
                handle_s=0.5,
            )
        spine = list(root.find(".//spine"))
        self.assertEqual([el.tag for el in spine], ["asset-clip", "transition", "asset-clip", "transition", "asset-clip"])
        c1, t1, c2, t2, c3 = spine
        # Clips: 12 s, 10 s, 8 s visible, back to back; each starts half a second into its file.
        self.assertEqual([c.get("offset") for c in (c1, c2, c3)], ["0s", _seconds_to_fcpxml(12), _seconds_to_fcpxml(22)])
        self.assertEqual({c.get("start") for c in (c1, c2, c3)}, {_seconds_to_fcpxml(0.5)})
        self.assertEqual(c2.get("duration"), _seconds_to_fcpxml(10))
        self.assertEqual((t1.get("offset"), t1.get("duration")), (_seconds_to_fcpxml(11.5), _seconds_to_fcpxml(1)))
        self.assertEqual(t2.get("offset"), _seconds_to_fcpxml(21.5))
        effects = {e.get("id"): e.get("uid") for e in root.findall("./resources/effect")}
        self.assertEqual(effects[t1.find("filter-video").get("ref")], CROSS_DISSOLVE_UID)
        self.assertEqual(effects[t1.find("filter-audio").get("ref")], AUDIO_CROSSFADE_UID)
        # Captions in the hidden frames are dropped; one crossing the edge is cut to it.
        titles = c1.findall("title")  # one per spoken word (word highlight)
        self.assertEqual({"".join(r.text for r in t.findall("text/text-style")) for t in titles}, {"EDGE CUT"})
        self.assertEqual(titles[0].get("offset"), _seconds_to_fcpxml(0.5))

    def test_single_clip_has_no_transition(self) -> None:
        with patch("services.resolve_export._safe_duration", return_value=10.0):
            root = _build_fcpxml(
                project_name="p", clip_paths=[Path("/tmp/Clip001.mp4")], music=None, timeline_duration_s=9.0, handle_s=0.5
            )
        self.assertIsNone(root.find(".//transition"))
        self.assertIsNone(root.find("./resources/effect"))

    def test_subtitles_follow_the_visible_timeline(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "captions.srt"
            _write_captions_srt(
                path, [[Caption(1.0, 2.0, "one")], [Caption(0.5, 1.5, "two")]], [13.0, 11.0], handle_s=0.5
            )
            text = path.read_text(encoding="utf-8")
        self.assertIn("00:00:00,500 --> 00:00:01,500\none", text)
        self.assertIn("00:00:12,000 --> 00:00:13,000\ntwo", text)


class TestBackgroundBlend(unittest.TestCase):
    def test_filter_fades_are_centred_on_the_cuts(self) -> None:
        graph = background_join_filter([13.0, 11.0, 9.0], 0.5)
        self.assertIn("[0:v]trim=start=0.500:end=13.000", graph)
        self.assertIn("[2:v]trim=start=0.000:end=8.500", graph)
        self.assertIn("xfade=transition=fade:duration=1.000:offset=11.500", graph)
        self.assertIn("xfade=transition=fade:duration=1.000:offset=21.500", graph)

    def test_rendered_background_is_exactly_timeline_long(self) -> None:
        with TemporaryDirectory() as tmp:
            sources = []
            for i, (dur, colour) in enumerate([(4.0, "red"), (3.0, "blue")]):
                src = Path(tmp) / f"bg{i}.mp4"
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                     f"color=c={colour}:s=64x112:r=30:d={dur}", "-pix_fmt", "yuv420p", str(src)],
                    check=True,
                )
                sources.append(src)
            out = join_backgrounds(sources, Path(tmp) / "Background.mp4", durations_s=[4.0, 3.0], handle_s=0.5)
            duration = float(
                subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
                    check=True, capture_output=True, text=True,
                ).stdout
            )
        self.assertAlmostEqual(duration, 3.0 + 2.0, delta=0.05)  # 3 s + 2 s visible


if __name__ == "__main__":
    unittest.main()
