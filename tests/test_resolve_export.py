from __future__ import annotations

import json
from dataclasses import replace
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from models.project import ClipSegment, MusicChoice, ProjectState, VerseChoice
from services.resolve_export import (
    _build_fcpxml,
    _seconds_to_fcpxml,
    _write_captions_srt,
    _write_verse_srt,
    generate_resolve_project,
)
from services.resolve_template import template_available
from services.caption_style import get_caption_style
from services.reel_layout import VerseLine
from services.transcribe import Caption, Word


class TestResolveExportHelpers(unittest.TestCase):
    def test_seconds_to_fcpxml(self) -> None:
        self.assertEqual(_seconds_to_fcpxml(0), "0s")
        self.assertEqual(_seconds_to_fcpxml(1.0), "3000/3000s")
        self.assertEqual(_seconds_to_fcpxml(2.0), "6000/3000s")

    def test_write_verse_srt(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "verse.srt"
            _write_verse_srt(path, "John 3:16", "For God so loved the world.", 5.0)
            text = path.read_text(encoding="utf-8")
            self.assertIn("John 3:16", text)
            self.assertIn("For God so loved the world.", text)
            self.assertIn("-->", text)


class TestResolveCaptions(unittest.TestCase):
    clip_captions = [
        [Caption(0.5, 1.5, "Grace upon grace")],
        [Caption(0.25, 1.0, "Amen")],
    ]

    def test_captions_srt_uses_timeline_time(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "captions.srt"
            count = _write_captions_srt(path, self.clip_captions, [4.0, 3.0])
            text = path.read_text(encoding="utf-8")
        self.assertEqual(count, 2)
        self.assertIn("00:00:00,500 --> 00:00:01,500\nGrace upon grace", text)
        # Second clip starts at 4.0s on the timeline.
        self.assertIn("00:00:04,250 --> 00:00:05,000\nAmen", text)

    def test_fcpxml_titles_connected_to_their_clip(self) -> None:
        clips = [Path("/tmp/Clip001.mp4"), Path("/tmp/Clip002.mp4")]
        with patch("services.resolve_export._safe_duration", side_effect=[4.0, 3.0]):
            root = _build_fcpxml(
                project_name="p",
                clip_paths=clips,
                music=None,
                timeline_duration_s=7.0,
                clip_captions=self.clip_captions,
                caption_style=get_caption_style("Clean white"),
            )
        effects = root.findall("./resources/effect")
        self.assertEqual(len(effects), 1)
        spine_clips = root.findall(".//spine/asset-clip")
        first, second = spine_clips
        t1 = first.find("title")
        self.assertEqual(t1.get("ref"), effects[0].get("id"))
        self.assertEqual(t1.get("lane"), "1")
        # Clip-relative: the parent asset-clip starts at 0s.
        self.assertEqual(t1.get("offset"), _seconds_to_fcpxml(0.5))
        self.assertEqual(t1.get("duration"), _seconds_to_fcpxml(1.0))
        self.assertEqual(t1.find("text/text-style").text, "Grace upon grace")
        # Resolve only honours caption placement given as a clip transform.
        self.assertEqual(t1.find("adjust-transform").get("position"), "0 -25")
        t2 = second.find("title")
        self.assertEqual(t2.get("offset"), _seconds_to_fcpxml(0.25))
        self.assertEqual(t2.find("text/text-style").text, "Amen")
        ids = [d.get("id") for d in root.iter("text-style-def")]
        self.assertEqual(len(ids), len(set(ids)))

    def test_word_highlight_titles_move_word_by_word(self) -> None:
        words = (Word(0.0, 0.3, " Turn"), Word(0.3, 0.5, " with"), Word(0.5, 1.0, " me."))
        caption = Caption(0.0, 1.2, "Turn with me", words)
        with patch("services.resolve_export._safe_duration", return_value=4.0):
            root = _build_fcpxml(
                project_name="p",
                clip_paths=[Path("/tmp/Clip001.mp4")],
                music=None,
                timeline_duration_s=4.0,
                clip_captions=[[caption]],
                caption_style=get_caption_style("Word highlight"),
            )
        titles = root.findall(".//title")
        self.assertEqual(len(titles), 3)
        frames = [
            (int(t.get("offset", "0s").split("/")[0].rstrip("s")) // 100,
             int(t.get("duration").split("/")[0]) // 100)
            for t in titles
        ]
        # Contiguous in whole frames: 0-9, 9-15, 15-36 (caption end 1.2s).
        self.assertEqual(frames, [(0, 9), (9, 6), (15, 21)])
        for i, title in enumerate(titles):
            runs = title.findall("text/text-style")
            self.assertEqual("".join(r.text for r in runs), "TURN WITH ME")
            colors = {
                d.get("id"): d.find("text-style").get("fontColor") for d in title.findall("text-style-def")
            }
            lit = [r.text.strip() for r in runs if colors[r.get("ref")] != "1 1 1 1"]
            self.assertEqual(lit, [["TURN", "WITH", "ME"][i]])
            self.assertEqual(title.find("adjust-transform").get("position"), "0 -25")
        ids = [d.get("id") for d in root.iter("text-style-def")]
        self.assertEqual(len(ids), len(set(ids)))

    def test_long_caption_wraps_onto_two_balanced_lines(self) -> None:
        from services.resolve_export import _line_break_index

        self.assertIsNone(_line_break_index(["TURN", "WITH", "ME"], 16))
        self.assertEqual(_line_break_index(["TO", "1", "CORINTHIANS", "13"], 16), 1)
        self.assertEqual(_line_break_index(["THE", "HOLY", "SPIRIT", "IS", "HERE"], 16), 1)
        caption = Caption(0.0, 1.0, "The Holy Spirit is here")
        with patch("services.resolve_export._safe_duration", return_value=4.0):
            root = _build_fcpxml(
                project_name="p",
                clip_paths=[Path("/tmp/Clip001.mp4")],
                music=None,
                timeline_duration_s=4.0,
                clip_captions=[[caption]],
                caption_style=replace(get_caption_style("Clean white"), font_size=64, max_line_chars=16),
            )
        # Resolve drops line breaks, so each line is its own title, stacked by lane/position.
        line1, line2 = root.findall(".//title")
        self.assertEqual("".join(r.text for r in line1.findall("text/text-style")), "The Holy")
        self.assertEqual("".join(r.text for r in line2.findall("text/text-style")), "Spirit is here")
        self.assertEqual((line1.get("lane"), line2.get("lane")), ("1", "2"))
        self.assertEqual(line1.get("offset"), line2.get("offset"))
        self.assertEqual(line1.find("adjust-transform").get("position"), "0 -21.8")
        self.assertEqual(line2.find("adjust-transform").get("position"), "0 -28.2")

    def test_no_titles_without_captions(self) -> None:
        with patch("services.resolve_export._safe_duration", return_value=4.0):
            root = _build_fcpxml(
                project_name="p",
                clip_paths=[Path("/tmp/Clip001.mp4")],
                music=None,
                timeline_duration_s=4.0,
            )
        self.assertIsNone(root.find("./resources/effect"))
        self.assertIsNone(root.find(".//title"))


class TestGenerateResolveProject(unittest.TestCase):
    def test_template_available_in_repo(self) -> None:
        self.assertTrue(template_available())

    def test_generate_bundle(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "src_clip.mp4"
            clip.write_bytes(b"\x00" * 256)
            music = root / "bed.mp3"
            music.write_bytes(b"\x00" * 128)

            project = ProjectState(
                reel_layout=False,
                project_name="07.12.26",
                clip_paths=[clip],
                segments=[ClipSegment("00:00:01", "00:00:05", "Hope")],
                selected_verse=VerseChoice("John 3:16", "For God so loved the world."),
                selected_music=MusicChoice("Calm Piano", local_path=music),
            )
            out_dir = root / "export"

            with patch("services.resolve_export._safe_duration", return_value=4.0):
                with patch("services.resolve_export.subprocess.run") as ff:
                    ff.return_value = None
                    timeline = generate_resolve_project(project, output_dir=out_dir)

            self.assertTrue(timeline.is_file())
            self.assertEqual(timeline.suffix, ".fcpxml")
            self.assertTrue((out_dir / "media" / "Clip001.mp4").is_file())
            self.assertTrue((out_dir / "media" / "instrumental.mp3").is_file())
            self.assertTrue((out_dir / "verse.srt").is_file())
            self.assertTrue((out_dir / "verse.txt").is_file())
            self.assertTrue((out_dir / "OPEN_IN_RESOLVE.txt").is_file())

            manifest = json.loads((out_dir / "wordly_manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["wordly_export"])
            self.assertEqual(manifest["nle"], "davinci_resolve_free")
            self.assertEqual(manifest["interchange"], "fcpxml")

            root_el = ET.parse(timeline).getroot()
            self.assertIn("fcpxml", root_el.tag.lower())
            xml_text = timeline.read_text(encoding="utf-8")
            self.assertIn("Clip001", xml_text)
            self.assertIn("file://", xml_text)
            self.assertEqual(project.resolve_output_path, timeline)


    def test_music_fades_out_over_the_last_three_seconds(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "src_clip.mp4"
            clip.write_bytes(b"\x00" * 256)
            music = root / "bed.mp3"
            music.write_bytes(b"\x00" * 128)
            project = ProjectState(
                reel_layout=False,
                project_name="fade",
                clip_paths=[clip],
                segments=[ClipSegment("00:00:01", "00:00:21", "Hope")],
                selected_verse=VerseChoice("John 3:16", "For God so loved the world."),
                selected_music=MusicChoice("Calm Piano", local_path=music),
            )
            calls: list[list[str]] = []

            def fake_run(cmd, **_kw):
                calls.append(cmd)
                Path(cmd[-1]).write_bytes(b"aac")  # ffmpeg wrote its output

            durations = {"Clip001": 21.0, "instrumental": 180.0}
            with patch(
                "services.resolve_export._safe_duration",
                side_effect=lambda p, **_k: durations.get(Path(p).stem, 21.0),
            ), patch("services.resolve_export.subprocess.run", side_effect=fake_run):
                timeline = generate_resolve_project(project, output_dir=root / "out")

            fade = next(c for c in calls if any("afade" in a for a in c))
            # The reel is 20 s (21 s minus the hidden half second at each end).
            self.assertEqual(fade[fade.index("-t") + 1], "20.000")
            self.assertEqual(fade[fade.index("-af") + 1], "afade=t=out:st=17.000:d=3.000")
            assets = ET.parse(timeline).getroot().findall("./resources/asset")
            self.assertTrue(any(a.get("src", "").endswith("media/instrumental_fade.m4a") for a in assets))

    def test_generate_uses_reviewed_captions_without_transcribing(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "src_clip.mp4"
            clip.write_bytes(b"\x00" * 256)
            music = root / "bed.mp3"
            music.write_bytes(b"\x00" * 128)
            project = ProjectState(
                reel_layout=False,
                project_name="reviewed",
                clip_paths=[clip],
                selected_verse=VerseChoice("John 3:16", "For God so loved the world."),
                selected_music=MusicChoice("Calm Piano", local_path=music),
                auto_captions=True,
                clip_captions=[[Caption(0.0, 1.0, "Edited by hand")]],
                captions_source=[clip],
            )
            out_dir = root / "export"
            with patch("services.resolve_export._safe_duration", return_value=4.0), patch(
                "services.resolve_export.subprocess.run"
            ), patch("services.resolve_export.transcribe_clips") as tc:
                timeline = generate_resolve_project(project, output_dir=out_dir)
            tc.assert_not_called()
            self.assertIn("EDITED", timeline.read_text(encoding="utf-8"))

    def test_generate_reel_layout_adds_background_and_verse(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "src_clip.mp4"
            clip.write_bytes(b"\x00" * 256)
            music = root / "bed.mp3"
            music.write_bytes(b"\x00" * 128)
            project = ProjectState(
                project_name="reel",
                clip_paths=[clip],
                selected_verse=VerseChoice("John 3:16", "For God so loved the world."),
                selected_music=MusicChoice("Calm Piano", local_path=music),
            )
            self.assertTrue(project.reel_layout)
            out_dir = root / "export"

            def fake_bg(src, dest, pick=None, camera=None):
                dest.write_bytes(b"bg")
                return dest

            def fake_main(src, dest, camera):
                dest.write_bytes(b"main")
                return dest

            ratios: list[float] = []
            with patch("services.resolve_export._safe_duration", return_value=4.0), patch(
                "services.resolve_export.subprocess.run"
            ), patch("services.resolve_export.render_background", side_effect=fake_bg), patch(
                "services.resolve_export.render_main", side_effect=fake_main
            ), patch(
                "services.resolve_export.verse_lines",
                return_value=[VerseLine("John 3:16", True, -9.5), VerseLine("For God so loved the world.", False, -12.0)],
            ), patch(
                "services.resolve_export.speaker_track.vision_available", return_value=True
            ), patch("services.resolve_export.speaker_track.analyze", return_value=[(0.5, 0.5, 0.6, 0.4, 0.3)]), patch(
                "services.resolve_export.stabilize_if_shaky", side_effect=lambda src, dest: src
            ):
                timeline = generate_resolve_project(
                    project, output_dir=out_dir, progress_cb=lambda r, _m: ratios.append(r)
                )
            # The progress bar only moves forward.
            known = [r for r in ratios if r >= 0]
            self.assertEqual(known, sorted(known))

            root_el = ET.parse(timeline).getroot()
            clip_el = root_el.find(".//spine/asset-clip")
            connected = {el.get("name"): el for el in clip_el}
            bg = connected["Background"]
            self.assertEqual((bg.tag, bg.get("lane")), ("asset-clip", "-1"))
            # The clip hides half a second at each end (for fades); connected items start with it.
            self.assertEqual(clip_el.get("start"), _seconds_to_fcpxml(0.5))
            self.assertEqual(clip_el.get("duration"), _seconds_to_fcpxml(3.0))
            self.assertEqual(bg.get("offset"), clip_el.get("start"))
            self.assertEqual(connected["Instrumental"].get("offset"), clip_el.get("start"))
            # The verse is one editable title with line breaks, on a single track.
            verse_titles = [t for t in root_el.iter("title") if t.get("name") == "Bible verse"]
            self.assertEqual(len(verse_titles), 1)
            verse = verse_titles[0]
            self.assertEqual((verse.tag, verse.get("lane")), ("title", "3"))
            runs = verse.findall("text/text-style")
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0].text, "John 3:16\nFor God so loved the world.")
            self.assertEqual(verse.find("adjust-transform").get("position"), "0 -10.75")
            style = verse.find("text-style-def/text-style")
            self.assertEqual(
                (style.get("font"), style.get("fontFace"), style.get("fontSize")), ("Outfit", "SemiBold", "18")
            )
            self.assertIsNone(root_el.find("./resources/format[@name='FFVideoFormatRateUndefined']"))
            self.assertEqual(connected["Instrumental"].get("lane"), "-2")
            # Background song sits 10 dB down, as an editable clip volume.
            self.assertEqual(connected["Instrumental"].find("adjust-volume").get("amount"), "-10dB")
            assets = {a.get("id"): a for a in root_el.findall("./resources/asset")}
            self.assertTrue(assets[bg.get("ref")].get("src").endswith("media/Background.mp4"))
            # The spine uses the zoomed head-to-waist render, which keeps the audio.
            main = assets[clip_el.get("ref")]
            self.assertTrue(main.get("src").endswith("media/Clip001_main.mp4"))
            self.assertEqual(main.get("hasAudio"), "1")
            self.assertEqual(assets[bg.get("ref")].get("hasAudio"), "0")
            self.assertFalse(any("verse.png" in (a.get("src") or "") for a in assets.values()))
            self.assertFalse((out_dir / "media" / "verse.png").exists())

    def test_generate_bundle_with_auto_captions(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "src_clip.mp4"
            clip.write_bytes(b"\x00" * 256)
            music = root / "bed.mp3"
            music.write_bytes(b"\x00" * 128)
            project = ProjectState(
                reel_layout=False,
                project_name="cap",
                clip_paths=[clip],
                selected_verse=VerseChoice("John 3:16", "For God so loved the world."),
                selected_music=MusicChoice("Calm Piano", local_path=music),
                auto_captions=True,
            )
            out_dir = root / "export"
            captions = [[Caption(0.0, 1.0, "He is risen")]]
            with patch("services.resolve_export._safe_duration", return_value=4.0), patch(
                "services.resolve_export.subprocess.run"
            ), patch("services.resolve_export.transcribe_clips", return_value=captions) as tc:
                timeline = generate_resolve_project(project, output_dir=out_dir)

            tc.assert_called_once()
            # Default "Word highlight" style is all caps.
            self.assertIn("HE IS RISEN", (out_dir / "captions.srt").read_text(encoding="utf-8"))
            self.assertIn("RISEN", timeline.read_text(encoding="utf-8"))
            manifest_style = json.loads((out_dir / "wordly_manifest.json").read_text(encoding="utf-8"))["caption_style"]
            self.assertEqual(manifest_style, "Sermon reel")
            manifest = json.loads((out_dir / "wordly_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["captions"], 1)
            self.assertIn("captions.srt", (out_dir / "OPEN_IN_RESOLVE.txt").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
