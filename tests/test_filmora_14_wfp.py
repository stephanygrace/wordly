from __future__ import annotations

import base64
import hashlib
import json
import re
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from models.project import ClipSegment, MusicChoice, ProjectState, VerseChoice
from services.filmora_14_wfp import (
    FILMORA_CLIP_DISPLAY_NAME_KEY,
    _detect_timeline_time_scale,
    _detect_timeline_trim_style,
    _filmora_file_url,
    _filmora_media_length_units,
    _patch_timeline_segments_only,
    _patch_timeline_music_clips,
    _patch_timeline_resources,
    _sync_media_json_duration,
    _uses_tl_source_trim_style,
    _validate_export_segments,
    TEMPLATE_SOURCE_VIDEO_ID,
    TEMPLATE_TIMELINE_HIGHLIGHT_FILE,
    TEMPLATE_TIMELINE_SOURCE_FILE,
    TEMPLATE_AUDIO_ID,
    TEMPLATE_TIMELINE_ID,
    TEMPLATE_VIDEO_ID,
    TemplateLayout,
    _STATIC_PATH_MARKERS,
    _filmora_path_str,
    _media_path_basename,
    _patch_medias_info,
    _patch_timeline_wesproj,
    _ensure_verse_script_layer,
    _compound_verse_duration_tl,
    _patch_script_buf_verse,
    _register_replacement,
    _replace_paths_in_text,
    _patch_timeline_track_layout,
    _rewrite_timeline_joined_filenames,
    _sanitize_timeline_filenames,
    _prepare_export_bundle,
    generate_wfp_from_template,
)


def _legacy_layout() -> TemplateLayout:
    return TemplateLayout(
        timeline_id=TEMPLATE_TIMELINE_ID,
        video_ids=(TEMPLATE_VIDEO_ID, TEMPLATE_SOURCE_VIDEO_ID),
        audio_ids=(TEMPLATE_AUDIO_ID,),
        image_ids=(),
        path_markers=_STATIC_PATH_MARKERS,
        source_video_id=TEMPLATE_SOURCE_VIDEO_ID,
        joined_video_id=TEMPLATE_VIDEO_ID,
    )


def _file_md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clip_display_name(clip: dict) -> str:
    for item in clip.get("userData") or []:
        if item.get("key") == FILMORA_CLIP_DISPLAY_NAME_KEY:
            return base64.b64decode(item["data"]).decode("utf-8").split("\x00")[0]
    return ""


class TestFilmoraPathPatching(unittest.TestCase):
    def test_filmora_path_str_keeps_unc_backslashes(self) -> None:
        from unittest.mock import patch

        with patch(
            "services.filmora_14_wfp.filmora_media_path",
            return_value="\\\\wsl$\\Ubuntu\\home\\stelle\\clip.mp4",
        ):
            self.assertEqual(
                _filmora_path_str(Path("/home/stelle/clip.mp4")),
                "\\\\wsl$\\Ubuntu\\home\\stelle\\clip.mp4",
            )

    def test_filmora_path_str_uses_forward_slashes_for_drive_paths(self) -> None:
        from unittest.mock import patch

        with patch(
            "services.filmora_14_wfp.filmora_media_path",
            return_value="C:\\Users\\demo\\clip.mp4",
        ):
            self.assertEqual(
                _filmora_path_str(Path("/mnt/c/Users/demo/clip.mp4")),
                "C:/Users/demo/clip.mp4",
            )

    def test_register_replacement_covers_file_url_variants(self) -> None:
        replacements: dict[str, str] = {}
        old = "D:/Any Video Converter/Format Convert/Facebook_1.mp4"
        new = "\\\\wsl$\\Ubuntu\\home\\stelle\\Projects\\wordly\\clips\\joined.mp4"
        _register_replacement(replacements, old, new)
        text = '{"file_name":"file:///D:/Any Video Converter/Format Convert/Facebook_1.mp4"}'
        patched = _replace_paths_in_text(text, replacements)
        self.assertNotIn("Facebook_1", patched)
        self.assertIn("joined.mp4", patched)

    def test_register_replacement_uses_forward_slashes_in_json(self) -> None:
        replacements: dict[str, str] = {}
        old = "C:/Users/steph/Downloads/Video/Facebook.mp4"
        new = r"C:\Users\steph\Desktop\wordly\clips\joined.mp4"
        _register_replacement(replacements, old, new)
        for value in replacements.values():
            self.assertNotIn("\\Users", value)
            self.assertIn("wordly/clips/joined.mp4", value.replace("\\", "/"))
        text = '{"filename":"file:/C:/Users/steph/Downloads/Video/Facebook.mp4"}'
        patched = _replace_paths_in_text(text, replacements)
        json.loads(patched)
        self.assertNotIn("\\", patched)

    def test_patch_medias_info_replaces_source_video_entry(self) -> None:
        old_source = "D:/Any Video Converter/Format Convert/Facebook_1.mp4"
        data = {
            "media_items": {
                TEMPLATE_VIDEO_ID: {
                    "download_url": "/tmp/old_joined.mp4",
                    "media_type": 8,
                    "name": "joined",
                },
                TEMPLATE_SOURCE_VIDEO_ID: {
                    "download_url": old_source,
                    "media_type": 8,
                    "name": "Facebook_1",
                },
                TEMPLATE_AUDIO_ID: {
                    "download_url": "/tmp/template_music.mp3",
                    "media_type": 4,
                    "name": "Template Song",
                },
            }
        }
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            joined = root / "joined.mp4"
            joined.write_bytes(b"\x00")
            sermon = root / "sermon.mp4"
            sermon.write_bytes(b"\x00")
            audio = root / "downloaded.m4a"
            audio.write_bytes(b"\x02")
            project = ProjectState(
                sermon_path=sermon,
                project_name="demo",
                selected_music=MusicChoice("Downloaded Piano", local_path=audio),
            )
            replacements = _patch_medias_info(
                data,
                project,
                _legacy_layout(),
                video_path=joined,
                source_video_path=joined,
                audio_path=audio,
                cover_path=None,
                video_duration_us=60_000_000,
                source_duration_us=60_000_000,
                audio_duration_us=180_000_000,
            )
            self.assertNotEqual(
                data["media_items"][TEMPLATE_SOURCE_VIDEO_ID]["download_url"],
                old_source,
            )
            self.assertIn(old_source, replacements)
            self.assertIn("joined.mp4", data["media_items"][TEMPLATE_SOURCE_VIDEO_ID]["download_url"])
            self.assertEqual(data["media_items"][TEMPLATE_AUDIO_ID]["name"], "Downloaded Piano")
            self.assertIn("downloaded.m4a", data["media_items"][TEMPLATE_AUDIO_ID]["download_url"])


class TestPatchTimelineResources(unittest.TestCase):
    def test_keeps_cover_image_separate_from_video(self) -> None:
        data = {
            "resources": [
                {"filename": "file:/C:/old/Sunday Service.mp4", "mediaLength": 999},
                {"filename": "file:/C:/old/cover.jpg", "mediaLength": 0},
                {"filename": "file:/C:/old/bed.mp3", "mediaLength": 999},
            ]
        }
        video = Path("C:/wordly/clips/joined.mp4")
        audio = Path("C:/wordly/music/bed.mp3")
        source = Path("C:/wordly/media/source_sermon.mp4")
        _patch_timeline_resources(
            data,
            source_path=source,
            joined_path=video,
            audio_path=audio,
            cover_path=None,
            source_duration_us=3_600_000_000,
            joined_duration_us=120_000_000,
            audio_duration_us=180_000_000,
        )
        self.assertIn("source_sermon.mp4", data["resources"][0]["filename"])
        self.assertIn("cover.jpg", data["resources"][1]["filename"])
        self.assertIn("bed.mp3", data["resources"][2]["filename"])
        self.assertEqual(data["resources"][0]["mediaLength"], 36_000_000_000)
        self.assertEqual(data["resources"][2]["mediaLength"], 1_800_000_000)


class TestSanitizeTimelineFilenames(unittest.TestCase):
    def test_normalizes_file_triple_slash(self) -> None:
        raw = '{"filename":"file:///C:/wordly/clips/joined.mp4"}'
        fixed = _sanitize_timeline_filenames(raw)
        self.assertIn("file:/C:/wordly/clips/joined.mp4", fixed)
        self.assertNotIn("file:///", fixed)

    def test_normalizes_mac_triple_slash_on_darwin(self) -> None:
        import sys

        if sys.platform != "darwin":
            self.skipTest("macOS Filmora URL form")
        raw = '{"filename":"file:///Users/stelle/clips/joined.mp4"}'
        fixed = _sanitize_timeline_filenames(raw)
        self.assertIn('"filename":"file://Users/stelle/clips/joined.mp4"', fixed)
        self.assertNotIn("file:///", fixed)

    def test_filmora_file_url_mac_format(self) -> None:
        import sys

        if sys.platform != "darwin":
            self.skipTest("macOS Filmora URL form")
        url = _filmora_file_url(Path("/Users/stelle/exports/demo/media/source_sermon.mp4"))
        self.assertEqual(
            url,
            "file://Users/stelle/exports/demo/media/source_sermon.mp4",
        )

    def test_fixes_backslashes_that_break_json(self) -> None:
        broken = (
            '{"filename":"file:/C:\\Users\\steph\\Desktop\\wordly\\assets\\music\\'
            'One Hour of Piano Hymns.m4a","inPoint":0,"outPoint":1}'
        )
        fixed = _sanitize_timeline_filenames(broken)
        json.loads(fixed)
        self.assertNotIn("\\Piano", fixed)
        self.assertIn("file:/C:/Users/steph/Desktop/wordly/assets/music/One Hour of Piano Hymns.m4a", fixed)


class TestRewriteTimelineJoinedFilenames(unittest.TestCase):
    def test_rewrites_only_joined_reel_not_sermon_segments(self) -> None:
        joined = Path("C:/wordly/clips/highlights_joined.mp4")
        source = Path("C:/wordly/media/source_sermon.mp4")
        sample = (
            '{"filename":"file:/D:/copy_BA7B4846-4A17-4F94-BBBB-CC9367FE3F0F.mov",'
            '"inPoint":0,"outPoint":1055054000},'
            '{"filename":"file:/D:/Video/Sunday Service.mp4",'
            '"inPoint":40465425000,"outPoint":40636596000}'
        )
        patched = _rewrite_timeline_joined_filenames(sample, joined, _STATIC_PATH_MARKERS)
        self.assertIn("highlights_joined.mp4", patched)
        self.assertIn("Sunday Service.mp4", patched)
        self.assertNotIn("copy_BA7B", patched)


class TestExportValidation(unittest.TestCase):
    def test_media_length_units_are_ten_x_microseconds(self) -> None:
        self.assertEqual(_filmora_media_length_units(3_000_000), 30_000_000)

    def test_sync_media_json_duration(self) -> None:
        payload = {
            "sourceInfo": {
                "basicInfo": {"mediaLength": 1},
                "vidStreamInfos": [{"streamLength": 1}],
            }
        }
        _sync_media_json_duration(payload, 5_000_000)
        self.assertEqual(payload["sourceInfo"]["basicInfo"]["mediaLength"], 50_000_000)
        self.assertEqual(payload["sourceInfo"]["vidStreamInfos"][0]["streamLength"], 50_000_000)

    def test_rejects_segments_past_source_duration(self) -> None:
        project = ProjectState(
            segments=[ClipSegment("01:20:00", "01:25:00")],
        )
        with self.assertRaises(ValueError) as ctx:
            _validate_export_segments(
                project,
                source_path=Path("C:/media/joined.mp4"),
                joined_path=Path("C:/media/joined.mp4"),
                source_duration_us=180_000_000,
            )
        self.assertIn("joined highlights", str(ctx.exception))


class TestMediaPathBasename(unittest.TestCase):
    def test_mac_absolute_path(self) -> None:
        path = "/Users/stelle/Projects/wordly/exports/06.28.26/media/source_sermon.mp4"
        self.assertEqual(_media_path_basename(path), "source_sermon.mp4")

    def test_file_url(self) -> None:
        url = "file:///Users/stelle/Projects/wordly/exports/06.28.26/media/source_sermon.mp4"
        self.assertEqual(_media_path_basename(url), "source_sermon.mp4")

    def test_wsl_unc_path(self) -> None:
        unc = r"\\wsl$\Ubuntu\home\stelle\exports\06.28.26\media\source_sermon.mp4"
        self.assertEqual(_media_path_basename(unc), "source_sermon.mp4")


class TestTimelineTrimStyle(unittest.TestCase):
    def test_detects_tl_source_trim_from_sermon_template_clips(self) -> None:
        clip = {
            "filename": "file:/C:/templates/video.mp4",
            "inPoint": 0,
            "outPoint": 5_677_286_000,
            "tlBegin": 0,
            "tlEnd": 119_452_667,
            "type": 2,
        }
        self.assertTrue(_uses_tl_source_trim_style(clip))
        data = {
            "timelineInfos": [
                {
                    "trackInfos": [
                        {
                            "clipList": [
                                clip,
                                {
                                    **clip,
                                    "tlBegin": 119_452_667,
                                    "tlEnd": 247_580_667,
                                },
                            ]
                        }
                    ]
                }
            ]
        }
        self.assertEqual(_detect_timeline_trim_style(data), "tl_source_trim")

    def test_detects_source_in_out_from_new_sermon_template_clips(self) -> None:
        clip = {
            "filename": "file:/C:/templates/video.mp4",
            "inPoint": 0,
            "outPoint": 119_452_667,
            "tlBegin": 0,
            "tlEnd": 119_452_667,
            "type": 2,
        }
        self.assertFalse(_uses_tl_source_trim_style(clip))
        data = {
            "resources": [
                {
                    "filename": "file:/C:/templates/video.mp4",
                    "mediaLength": 5_677_286_000,
                }
            ],
            "timelineInfos": [
                {
                    "trackInfos": [
                        {
                            "clipList": [
                                clip,
                                {
                                    **clip,
                                    "tlBegin": 119_452_667,
                                    "tlEnd": 247_580_667,
                                    "outPoint": 128_128_000,
                                },
                            ]
                        }
                    ]
                }
            ],
        }
        self.assertEqual(_detect_timeline_trim_style(data), "source_in_out")
        self.assertEqual(_detect_timeline_time_scale(data), 10_000_000)

    def test_sermon_template_uses_100ns_scale(self) -> None:
        template = Path(__file__).resolve().parents[1] / "assets/filmora_templates/sermon-highlights.wfp"
        if not template.is_file():
            self.skipTest("sermon-highlights template missing")
        with zipfile.ZipFile(template) as zf:
            pi = json.loads(zf.read("ProjectFolder/project_info.json"))
            tl = json.loads(
                zf.read(f'ProjectFolder/Medias/{pi["timeline_mediaId"]}/timeline.wesproj')
            )
        self.assertEqual(_detect_timeline_time_scale(tl), 10_000_000)


class TestPatchTimelineMusicClips(unittest.TestCase):
    def test_points_music_clip_at_exported_instrumental(self) -> None:
        data = {
            "resources": [
                {
                    "filename": "file:/C:/old/video.mp4",
                    "mediaLength": 5_677_286_000,
                    "sourceUuid": "video-uuid",
                },
                {
                    "filename": "file:/C:/old/music.mp3",
                    "mediaLength": 275_086_803,
                    "sourceUuid": "music-uuid",
                }
            ],
            "timelineInfos": [
                {
                    "trackInfos": [
                        {
                            "clipList": [
                                {
                                    "filename": "file:/C:/old/video.mp4",
                                    "inPoint": 0,
                                    "outPoint": 119_452_667,
                                    "tlBegin": 0,
                                    "tlEnd": 119_452_667,
                                    "sourceUuid": "video-uuid",
                                    "type": 2,
                                },
                                {
                                    "filename": "file:/C:/old/music.mp3",
                                    "inPoint": 0,
                                    "outPoint": 275_086_803,
                                    "tlBegin": 0,
                                    "tlEnd": 275_086_803,
                                    "sourceUuid": "music-uuid",
                                    "type": 2,
                                }
                            ]
                        }
                    ]
                }
            ],
        }
        audio = Path("C:/wordly/exports/demo/media/instrumental.mp3")
        _patch_timeline_resources(
            data,
            source_path=Path("C:/wordly/source.mp4"),
            joined_path=Path("C:/wordly/joined.mp4"),
            audio_path=audio,
            cover_path=None,
            source_duration_us=3_600_000_000,
            joined_duration_us=60_000_000,
            audio_duration_us=180_000_000,
        )
        _patch_timeline_music_clips(
            data,
            audio_path=audio,
            audio_duration_us=180_000_000,
            highlight_duration_us=60_000_000,
            display_name="Downloaded Piano",
        )
        clips = data["timelineInfos"][0]["trackInfos"][0]["clipList"]
        music = clips[1]
        self.assertIn("instrumental.mp3", music["filename"])
        self.assertEqual(_clip_display_name(music), "Downloaded Piano")
        self.assertEqual(music["outPoint"], 600_000_000)
        self.assertEqual(music["tlEnd"], 600_000_000)


class TestPatchTimelineSegmentsOnly(unittest.TestCase):
    def test_removes_unused_template_clip(self) -> None:
        """Extra template slots are dropped (not parked at zero span)."""
        data = {
            "resources": [
                {"filename": "file:/C:/old/video.mp4", "mediaLength": 5_677_286_000}
            ],
            "timelineInfos": [
                {
                    "trackInfos": [
                        {
                            "clipList": [
                                {
                                    "filename": "file:/C:/old/video.mp4",
                                    "inPoint": 0,
                                    "outPoint": 119_452_667,
                                    "tlBegin": 0,
                                    "tlEnd": 119_452_667,
                                    "type": 2,
                                },
                                {
                                    "filename": "file:/C:/old/video.mp4",
                                    "inPoint": 0,
                                    "outPoint": 128_128_000,
                                    "tlBegin": 119_452_667,
                                    "tlEnd": 247_580_667,
                                    "type": 2,
                                },
                            ]
                        }
                    ]
                }
            ],
        }
        project = ProjectState(segments=[ClipSegment("00:01:00", "00:03:40")])
        _patch_timeline_segments_only(
            data,
            project=project,
            source_url="file:/C:/wordly/source_sermon.mp4",
            source_duration_us=3_600_000_000,
        )
        clips = data["timelineInfos"][0]["trackInfos"][0]["clipList"]
        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0]["tlBegin"], 0)
        self.assertEqual(clips[0]["tlEnd"], 1_600_000_000)
        self.assertGreater(clips[0]["tlEnd"], clips[0]["tlBegin"])

    def test_two_segments_patch_both_clips(self) -> None:
        data = {
            "resources": [
                {"filename": "file:/C:/old/video.mp4", "mediaLength": 5_677_286_000}
            ],
            "timelineInfos": [
                {
                    "trackInfos": [
                        {
                            "clipList": [
                                {
                                    "filename": "file:/C:/old/video.mp4",
                                    "inPoint": 0,
                                    "outPoint": 119_452_667,
                                    "tlBegin": 0,
                                    "tlEnd": 119_452_667,
                                    "type": 2,
                                },
                                {
                                    "filename": "file:/C:/old/video.mp4",
                                    "inPoint": 0,
                                    "outPoint": 128_128_000,
                                    "tlBegin": 119_452_667,
                                    "tlEnd": 247_580_667,
                                    "type": 2,
                                },
                            ]
                        }
                    ]
                }
            ],
        }
        project = ProjectState(
            segments=[
                ClipSegment("01:30:00", "01:31:00"),
                ClipSegment("00:10:00", "00:14:18"),
            ],
        )
        _patch_timeline_segments_only(
            data,
            project=project,
            source_url="file:/C:/wordly/source_sermon.mp4",
            source_duration_us=5_677_286_000,
        )
        clips = data["timelineInfos"][0]["trackInfos"][0]["clipList"]
        self.assertEqual(clips[0]["tlBegin"], 0)
        self.assertEqual(clips[0]["tlEnd"], 600_000_000)
        self.assertEqual(clips[1]["tlBegin"], 600_000_000)
        self.assertEqual(clips[1]["tlEnd"], 3_180_000_000)
        self.assertEqual(clips[1]["inPoint"], 6_000_000_000)
        self.assertEqual(clips[1]["outPoint"], 8_580_000_000)

    def test_names_segment_clips_clip_1_clip_2(self) -> None:
        data = {
            "resources": [
                {"filename": "file:/C:/old/video.mp4", "mediaLength": 5_677_286_000}
            ],
            "timelineInfos": [
                {
                    "trackInfos": [
                        {
                            "clipList": [
                                {
                                    "filename": "file:/C:/old/video.mp4",
                                    "inPoint": 0,
                                    "outPoint": 119_452_667,
                                    "tlBegin": 0,
                                    "tlEnd": 119_452_667,
                                    "type": 2,
                                },
                                {
                                    "filename": "file:/C:/old/video.mp4",
                                    "inPoint": 0,
                                    "outPoint": 128_128_000,
                                    "tlBegin": 119_452_667,
                                    "tlEnd": 247_580_667,
                                    "type": 2,
                                },
                            ]
                        }
                    ]
                }
            ],
        }
        project = ProjectState(
            segments=[
                ClipSegment("01:30:00", "01:31:00"),
                ClipSegment("00:10:00", "00:14:18"),
            ],
        )
        _patch_timeline_segments_only(
            data,
            project=project,
            source_url="file:/C:/wordly/source_sermon.mp4",
            source_duration_us=5_677_286_000,
        )
        clips = data["timelineInfos"][0]["trackInfos"][0]["clipList"]
        self.assertEqual(_clip_display_name(clips[0]), "Clip 1")
        self.assertEqual(_clip_display_name(clips[1]), "Clip 2")

    def test_uses_segment_label_when_set(self) -> None:
        data = {
            "resources": [
                {"filename": "file:/C:/old/video.mp4", "mediaLength": 5_677_286_000}
            ],
            "timelineInfos": [
                {
                    "trackInfos": [
                        {
                            "clipList": [
                                {
                                    "filename": "file:/C:/old/video.mp4",
                                    "inPoint": 0,
                                    "outPoint": 119_452_667,
                                    "tlBegin": 0,
                                    "tlEnd": 119_452_667,
                                    "type": 2,
                                },
                                {
                                    "filename": "file:/C:/old/video.mp4",
                                    "inPoint": 0,
                                    "outPoint": 128_128_000,
                                    "tlBegin": 119_452_667,
                                    "tlEnd": 247_580_667,
                                    "type": 2,
                                },
                            ]
                        }
                    ]
                }
            ],
        }
        project = ProjectState(
            segments=[
                ClipSegment("01:30:00", "01:31:00", label="Opening"),
                ClipSegment("00:10:00", "00:14:18"),
            ],
        )
        _patch_timeline_segments_only(
            data,
            project=project,
            source_url="file:/C:/wordly/source_sermon.mp4",
            source_duration_us=5_677_286_000,
        )
        clips = data["timelineInfos"][0]["trackInfos"][0]["clipList"]
        self.assertEqual(_clip_display_name(clips[0]), "Opening")
        self.assertEqual(_clip_display_name(clips[1]), "Clip 2")

    def test_retimes_template_dissolve_transitions(self) -> None:
        data = {
            "resources": [
                {"filename": "file:/C:/old/video.mp4", "mediaLength": 5_677_286_000}
            ],
            "timelineInfos": [
                {
                    "trackInfos": [
                        {
                            "clipList": [
                                {
                                    "filename": "file:/C:/old/video.mp4",
                                    "inPoint": 0,
                                    "outPoint": 119_452_667,
                                    "tlBegin": 0,
                                    "tlEnd": 119_452_667,
                                    "type": 2,
                                    "postTransition": {
                                        "display": "Dissolve",
                                        "tlBegin": 114_452_667,
                                        "tlEnd": 124_452_667,
                                    },
                                },
                                {
                                    "filename": "file:/C:/old/video.mp4",
                                    "inPoint": 0,
                                    "outPoint": 128_128_000,
                                    "tlBegin": 119_452_667,
                                    "tlEnd": 247_580_667,
                                    "type": 2,
                                    "postTransition": {
                                        "display": "Dissolve",
                                        "tlBegin": 237_580_667,
                                        "tlEnd": 247_580_667,
                                    },
                                },
                            ]
                        }
                    ]
                }
            ],
        }
        project = ProjectState(
            segments=[
                ClipSegment("01:30:00", "01:31:00"),
                ClipSegment("00:10:00", "00:14:18"),
            ],
        )
        _patch_timeline_segments_only(
            data,
            project=project,
            source_url="file:/C:/wordly/source_sermon.mp4",
            source_duration_us=5_677_286_000,
        )
        clips = data["timelineInfos"][0]["trackInfos"][0]["clipList"]
        self.assertEqual(clips[0]["postTransition"]["display"], "Dissolve")
        self.assertEqual(clips[0]["postTransition"]["tlBegin"], 595_000_000)
        self.assertEqual(clips[0]["postTransition"]["tlEnd"], 605_000_000)
        self.assertEqual(clips[1]["postTransition"]["tlBegin"], 3_170_000_000)
        self.assertEqual(clips[1]["postTransition"]["tlEnd"], 3_180_000_000)

    def test_dedupes_shared_effect_uids_between_template_slots(self) -> None:
        data = {
            "resources": [
                {"filename": "file:/C:/old/video.mp4", "mediaLength": 5_677_286_000}
            ],
            "timelineInfos": [
                {
                    "trackInfos": [
                        {
                            "clipList": [
                                {
                                    "filename": "file:/C:/old/video.mp4",
                                    "inPoint": 0,
                                    "outPoint": 119_452_667,
                                    "tlBegin": 0,
                                    "tlEnd": 119_452_667,
                                    "type": 2,
                                    "thisUId": "clip-a",
                                    "effectChainList": [
                                        {
                                            "effectList": [
                                                {
                                                    "id": "video/effect/transform",
                                                    "thisUId": "shared-effect",
                                                    "type": 3,
                                                }
                                            ],
                                            "name": "Basic",
                                        }
                                    ],
                                },
                                {
                                    "filename": "file:/C:/old/video.mp4",
                                    "inPoint": 0,
                                    "outPoint": 128_128_000,
                                    "tlBegin": 119_452_667,
                                    "tlEnd": 247_580_667,
                                    "type": 2,
                                    "thisUId": "clip-b",
                                    "effectChainList": [
                                        {
                                            "effectList": [
                                                {
                                                    "id": "video/effect/transform",
                                                    "thisUId": "shared-effect",
                                                    "type": 3,
                                                }
                                            ],
                                            "name": "Basic",
                                        }
                                    ],
                                },
                            ]
                        }
                    ]
                }
            ],
        }
        project = ProjectState(
            segments=[
                ClipSegment("00:01:00", "00:02:00"),
                ClipSegment("00:10:00", "00:11:00"),
            ],
        )
        _patch_timeline_segments_only(
            data,
            project=project,
            source_url="file:/C:/wordly/source_sermon.mp4",
            source_duration_us=5_677_286_000,
        )
        effect_uids = [
            effect["thisUId"]
            for clip in data["timelineInfos"][0]["trackInfos"][0]["clipList"]
            for chain in clip.get("effectChainList") or []
            for effect in chain.get("effectList") or []
        ]
        self.assertEqual(len(effect_uids), len(set(effect_uids)))

    def test_cloned_segment_slots_get_unique_effect_uids(self) -> None:
        data = {
            "resources": [
                {"filename": "file:/C:/old/video.mp4", "mediaLength": 5_677_286_000}
            ],
            "timelineInfos": [
                {
                    "trackInfos": [
                        {
                            "clipList": [
                                {
                                    "filename": "file:/C:/old/video.mp4",
                                    "inPoint": 0,
                                    "outPoint": 119_452_667,
                                    "tlBegin": 0,
                                    "tlEnd": 119_452_667,
                                    "type": 1,
                                    "thisUId": "clip-a",
                                    "effectChainList": [
                                        {
                                            "effectList": [
                                                {
                                                    "id": "video/effect/transform",
                                                    "thisUId": "effect-a",
                                                    "type": 3,
                                                }
                                            ],
                                            "name": "Basic",
                                        }
                                    ],
                                },
                                {
                                    "filename": "file:/C:/old/video.mp4",
                                    "inPoint": 0,
                                    "outPoint": 128_128_000,
                                    "tlBegin": 119_452_667,
                                    "tlEnd": 247_580_667,
                                    "type": 1,
                                    "thisUId": "clip-b",
                                    "effectChainList": [
                                        {
                                            "effectList": [
                                                {
                                                    "id": "video/effect/transform",
                                                    "thisUId": "effect-b",
                                                    "type": 3,
                                                }
                                            ],
                                            "name": "Basic",
                                        }
                                    ],
                                },
                            ]
                        }
                    ]
                }
            ],
        }
        project = ProjectState(
            segments=[
                ClipSegment("00:01:00", "00:02:00"),
                ClipSegment("00:10:00", "00:11:00"),
                ClipSegment("00:20:00", "00:21:00"),
                ClipSegment("00:30:00", "00:31:00"),
            ],
        )
        _patch_timeline_segments_only(
            data,
            project=project,
            source_url="file:/C:/wordly/source_sermon.mp4",
            source_duration_us=5_677_286_000,
        )
        clips = data["timelineInfos"][0]["trackInfos"][0]["clipList"]
        self.assertEqual(len(clips), 4)
        effect_uids: list[str] = []
        for clip in clips:
            for chain in clip.get("effectChainList") or []:
                for effect in chain.get("effectList") or []:
                    effect_uids.append(str(effect["thisUId"]))
        self.assertEqual(len(effect_uids), len(set(effect_uids)))

    def test_four_segments_keep_template_slot_order(self) -> None:
        """Clones append after template slot 2 so segment N maps to clip N."""
        data = {
            "resources": [
                {"filename": "file:/C:/old/video.mp4", "mediaLength": 5_677_286_000}
            ],
            "timelineInfos": [
                {
                    "trackInfos": [
                        {
                            "clipList": [
                                {
                                    "filename": "file:/C:/old/video.mp4",
                                    "inPoint": 0,
                                    "outPoint": 119_452_667,
                                    "tlBegin": 0,
                                    "tlEnd": 119_452_667,
                                    "type": 2,
                                    "thisUId": "clip-a",
                                },
                                {
                                    "filename": "file:/C:/old/video.mp4",
                                    "inPoint": 0,
                                    "outPoint": 128_128_000,
                                    "tlBegin": 119_452_667,
                                    "tlEnd": 247_580_667,
                                    "type": 2,
                                    "thisUId": "clip-b",
                                },
                            ]
                        }
                    ]
                }
            ],
        }
        project = ProjectState(
            segments=[
                ClipSegment("00:01:00", "00:02:00"),
                ClipSegment("00:10:00", "00:11:00"),
                ClipSegment("00:20:00", "00:21:00"),
                ClipSegment("00:30:00", "00:31:00"),
            ],
        )
        _patch_timeline_segments_only(
            data,
            project=project,
            source_url="file:/C:/wordly/source_sermon.mp4",
            source_duration_us=5_677_286_000,
        )
        clips = data["timelineInfos"][0]["trackInfos"][0]["clipList"]
        self.assertEqual(len(clips), 4)
        clip_uids = {c["thisUId"] for c in clips}
        self.assertEqual(len(clip_uids), 4)
        self.assertEqual(clips[0]["thisUId"], "clip-a")
        self.assertEqual(clips[0]["tlBegin"], 0)
        self.assertEqual(clips[1]["tlBegin"], 600_000_000)
        self.assertEqual(clips[2]["tlBegin"], 1_200_000_000)
        self.assertEqual(clips[3]["tlBegin"], 1_800_000_000)
        self.assertEqual(clips[3]["inPoint"], 18_000_000_000)
        self.assertEqual(clips[3]["outPoint"], 18_600_000_000)

    def test_three_segments_adds_third_clip_slot(self) -> None:
        data = {
            "resources": [
                {"filename": "file:/C:/old/video.mp4", "mediaLength": 5_677_286_000}
            ],
            "timelineInfos": [
                {
                    "trackInfos": [
                        {
                            "clipList": [
                                {
                                    "filename": "file:/C:/old/video.mp4",
                                    "inPoint": 0,
                                    "outPoint": 119_452_667,
                                    "tlBegin": 0,
                                    "tlEnd": 119_452_667,
                                    "type": 2,
                                    "thisUId": "clip-a",
                                    "sourceUuid": "res-1",
                                },
                                {
                                    "filename": "file:/C:/old/video.mp4",
                                    "inPoint": 0,
                                    "outPoint": 128_128_000,
                                    "tlBegin": 119_452_667,
                                    "tlEnd": 247_580_667,
                                    "type": 2,
                                    "thisUId": "clip-b",
                                    "sourceUuid": "res-1",
                                },
                            ]
                        }
                    ]
                }
            ],
        }
        project = ProjectState(
            segments=[
                ClipSegment("00:01:00", "00:02:00"),
                ClipSegment("00:10:00", "00:11:00"),
                ClipSegment("00:20:00", "00:21:00"),
            ],
        )
        _patch_timeline_segments_only(
            data,
            project=project,
            source_url="file:/C:/wordly/source_sermon.mp4",
            source_duration_us=5_677_286_000,
        )
        clips = data["timelineInfos"][0]["trackInfos"][0]["clipList"]
        self.assertEqual(len(clips), 3)
        uids = {c["thisUId"] for c in clips}
        self.assertEqual(len(uids), 3)
        self.assertEqual(clips[2]["tlBegin"], 1_200_000_000)
        self.assertEqual(clips[2]["tlEnd"], 1_800_000_000)
        self.assertEqual(clips[2]["inPoint"], 12_000_000_000)
        self.assertEqual(clips[2]["outPoint"], 12_600_000_000)
        effect_counts = [
            len(
                [
                    effect
                    for chain in clip.get("effectChainList") or []
                    for effect in chain.get("effectList") or []
                ]
            )
            for clip in clips
        ]
        self.assertEqual(effect_counts[0], effect_counts[1])
        self.assertEqual(effect_counts[0], effect_counts[2])
        for clip in clips:
            span = int(clip["tlEnd"]) - int(clip["tlBegin"])
            speed = clip.get("speed")
            if not isinstance(speed, dict) or "offsetEnd" not in speed:
                continue
            self.assertAlmostEqual(float(speed["offsetEnd"]), span / 10_000_000)

    def test_retimes_compound_verse_clip_to_highlight_span(self) -> None:
        data = {
            "timelineInfos": [
                {
                    "trackInfos": [
                        {
                            "clipList": [
                                {
                                    "filename": "file:/C:/old/video.mp4",
                                    "inPoint": 0,
                                    "outPoint": 999,
                                    "tlBegin": 0,
                                    "tlEnd": 999,
                                    "type": 2,
                                },
                                {
                                    "filename": "file:/C:/old/video.mp4",
                                    "inPoint": 0,
                                    "outPoint": 999,
                                    "tlBegin": 999,
                                    "tlEnd": 1998,
                                    "type": 2,
                                },
                            ]
                        },
                        {
                            "clipList": [
                                {
                                    "inPoint": 0,
                                    "outPoint": 5000,
                                    "tlBegin": 0,
                                    "tlEnd": 5000,
                                    "timelineId": 17,
                                    "type": 7,
                                }
                            ]
                        },
                    ]
                },
                {"timelineId": 17, "trackInfos": [{"clipList": []}]},
            ],
        }
        project = ProjectState(
            segments=[
                ClipSegment("00:01:00", "00:03:40"),
                ClipSegment("00:10:00", "00:14:18"),
            ]
        )
        timeline_end = _patch_timeline_segments_only(
            data,
            project=project,
            source_url="file:/C:/wordly/source_sermon.mp4",
            source_duration_us=3_600_000_000,
        )
        compound = data["timelineInfos"][0]["trackInfos"][1]["clipList"][0]
        self.assertGreater(timeline_end, 0)
        self.assertEqual(int(compound["tlEnd"]), timeline_end)
        self.assertEqual(
            _compound_verse_duration_tl(data, verse_timeline_id=17),
            timeline_end,
        )


class TestPatchScriptBufVerse(unittest.TestCase):
    def test_keeps_script_buf_valid_json_with_crlf_and_quotes(self) -> None:
        template_wfp = (
            Path(__file__).resolve().parents[1]
            / "assets"
            / "filmora_templates"
            / "sermon-highlights.wfp"
        )
        if not template_wfp.is_file():
            self.skipTest("sermon-highlights template missing")

        with zipfile.ZipFile(template_wfp) as zf:
            timeline_id = json.loads(zf.read("ProjectFolder/project_info.json"))[
                "timeline_mediaId"
            ]
            script_buf = json.loads(
                zf.read(f"ProjectFolder/Medias/{timeline_id}/timeline.wesproj")
            )["timelineInfos"][1]["trackInfos"][0]["clipList"][0]["scriptBuf"]

        patched = _patch_script_buf_verse(
            script_buf,
            "Psalm 19:7–8",
            '"The law of the Lord is perfect, refreshing the soul."',
        )
        parsed = json.loads(patched)
        self.assertIn("Psalm 19:7–8", parsed["Text"])
        self.assertIn("The law of the Lord is perfect", parsed["Text"])


class TestVerseCompoundTextSync(unittest.TestCase):
    def test_inner_text_matches_compound_clip_duration(self) -> None:
        data = {
            "timelineInfos": [
                {
                    "trackInfos": [
                        {
                            "clipList": [
                                {
                                    "inPoint": 0,
                                    "outPoint": 2_000_000_000,
                                    "tlBegin": 0,
                                    "tlEnd": 2_000_000_000,
                                    "timelineId": 17,
                                    "type": 7,
                                }
                            ]
                        }
                    ]
                },
                {
                    "timelineId": 17,
                    "trackInfos": [
                        {
                            "clipList": [
                                {
                                    "inPoint": 0,
                                    "outPoint": 1,
                                    "tlBegin": 0,
                                    "tlEnd": 1,
                                    "type": 4,
                                    "scriptBuf": '{"Text":"placeholder","CharData":"placeholder"}',
                                }
                            ]
                        }
                    ],
                },
            ],
        }
        project = ProjectState(
            segments=[ClipSegment("00:01:00", "00:03:40")],
            selected_verse=VerseChoice("John 3:16", "For God so loved the world."),
        )
        _ensure_verse_script_layer(
            data,
            project=project,
            joined_duration_us=160_000_000,
            time_scale=10_000_000,
        )
        compound = data["timelineInfos"][0]["trackInfos"][0]["clipList"][0]
        inner = data["timelineInfos"][1]["trackInfos"][0]["clipList"][0]
        self.assertEqual(int(inner["tlEnd"]), int(compound["tlEnd"]))
        self.assertIn("John 3:16", inner["scriptBuf"])


class TestPatchTimelineTrackLayout(unittest.TestCase):
    def test_tl_source_trim_keeps_in_at_zero(self) -> None:
        data = {
            "timelineInfos": [
                {
                    "trackInfos": [
                        {
                            "clipList": [
                                {
                                    "filename": "file:/C:/old/video.mp4",
                                    "inPoint": 0,
                                    "outPoint": 5_677_286_000,
                                    "tlBegin": 0,
                                    "tlEnd": 119_452_667,
                                    "type": 2,
                                },
                                {
                                    "filename": "file:/C:/old/video.mp4",
                                    "inPoint": 0,
                                    "outPoint": 5_677_286_000,
                                    "tlBegin": 119_452_667,
                                    "tlEnd": 247_580_667,
                                    "type": 2,
                                },
                            ]
                        }
                    ]
                }
            ]
        }
        project = ProjectState(
            segments=[
                ClipSegment("00:01:00", "00:03:40"),
                ClipSegment("00:10:00", "00:14:18"),
            ],
        )
        source = Path("C:/wordly/media/source_sermon.mp4")
        joined = Path("C:/wordly/media/highlights_joined.mp4")
        layout = TemplateLayout(
            timeline_id="{B9BCB4B4-70B1-478b-A5FE-F579F00C32E2}",
            video_ids=("{A22A93AF-2D28-4462-B206-C5096847873E}",),
            audio_ids=(),
            image_ids=(),
            path_markers=_STATIC_PATH_MARKERS,
            source_video_id="{A22A93AF-2D28-4462-B206-C5096847873E}",
            joined_video_id="{A22A93AF-2D28-4462-B206-C5096847873E}",
        )
        _patch_timeline_track_layout(
            data,
            project=project,
            layout=layout,
            source_path=source,
            joined_path=joined,
            audio_path=None,
            cover_path=None,
            source_duration_us=3_600_000_000,
            joined_duration_us=275_086_803,
            audio_duration_us=0,
        )
        clips = data["timelineInfos"][0]["trackInfos"][0]["clipList"]
        self.assertEqual(clips[0]["inPoint"], 0)
        self.assertEqual(clips[0]["outPoint"], 3_600_000_000)
        self.assertEqual(clips[0]["tlBegin"], 60_000_000)
        self.assertEqual(clips[0]["tlEnd"], 220_000_000)
        self.assertEqual(clips[1]["inPoint"], 0)
        self.assertEqual(clips[1]["outPoint"], 3_600_000_000)
        self.assertEqual(clips[1]["tlBegin"], 600_000_000)
        self.assertEqual(clips[1]["tlEnd"], 858_000_000)

    def test_lays_sermon_segments_on_timeline(self) -> None:
        data = {
            "timelineInfos": [
                {
                    "trackInfos": [
                        {
                            "clipList": [
                                {
                                    "filename": f"file:/C:/old/{TEMPLATE_TIMELINE_SOURCE_FILE}",
                                    "inPoint": 99,
                                    "outPoint": 100,
                                    "tlBegin": 0,
                                    "tlEnd": 1,
                                    "type": 1,
                                },
                                {
                                    "filename": f"file:/C:/old/{TEMPLATE_TIMELINE_SOURCE_FILE}",
                                    "inPoint": 99,
                                    "outPoint": 100,
                                    "tlBegin": 0,
                                    "tlEnd": 1,
                                    "type": 1,
                                },
                            ]
                        }
                    ]
                }
            ]
        }
        project = ProjectState(
            segments=[
                ClipSegment("00:01:00", "00:03:40"),
                ClipSegment("00:10:00", "00:14:18"),
            ],
        )
        source = Path("C:/wordly/media/source_sermon.mp4")
        joined = Path("C:/wordly/media/highlights_joined.mp4")
        layout = _legacy_layout()
        duration = _patch_timeline_track_layout(
            data,
            project=project,
            layout=layout,
            source_path=source,
            joined_path=joined,
            audio_path=None,
            cover_path=None,
            source_duration_us=3_600_000_000,
            joined_duration_us=418_000_000,
            audio_duration_us=0,
        )
        clips = data["timelineInfos"][0]["trackInfos"][0]["clipList"]
        self.assertIn("source_sermon.mp4", clips[0]["filename"])
        self.assertEqual(clips[0]["inPoint"], 60_000_000)
        self.assertEqual(clips[0]["outPoint"], 220_000_000)
        self.assertEqual(clips[0]["tlBegin"], 0)
        self.assertEqual(clips[0]["tlEnd"], 160_000_000)
        self.assertEqual(clips[1]["inPoint"], 600_000_000)
        self.assertEqual(clips[1]["tlBegin"], 160_000_000)
        self.assertEqual(clips[1]["tlEnd"], 160_000_000 + 258_000_000)
        self.assertEqual(duration, 418_000_000)


class TestPatchTimelineWesproj(unittest.TestCase):
    def test_patches_verse_text(self) -> None:
        sample = '\\"Text\\":\\"Psalm 1:1\\rOld verse\\"'
        project = ProjectState(
            segments=[ClipSegment("00:01:00", "00:03:40")],
            selected_verse=VerseChoice("John 3:16", "For God so loved the world."),
        )
        patched = _patch_timeline_wesproj(
            sample,
            project=project,
            layout=_legacy_layout(),
            source_path=Path("C:/wordly/media/source_sermon.mp4"),
            joined_path=Path("C:/wordly/media/highlights_joined.mp4"),
            audio_path=None,
            cover_path=None,
            source_duration_us=3_600_000_000,
            joined_duration_us=160_000_000,
            audio_duration_us=0,
            timeline_duration_us=160_000_000,
        )
        self.assertIn("John 3:16", patched)
        self.assertIn("For God so loved the world.", patched)


class TestExportMediaBundle(unittest.TestCase):
    def test_prepare_export_bundle_copies_media(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            joined = root / "joined.mp4"
            joined.write_bytes(b"\x00" * 128)
            sermon = root / "sermon.mp4"
            sermon.write_bytes(b"\x01" * 128)
            music = root / "bed.mp3"
            music.write_bytes(b"\x02" * 128)
            project = ProjectState(
                project_name="bundle-test",
                sermon_path=sermon,
                joined_clip_path=joined,
                selected_verse=VerseChoice("John 3:16", "For God so loved the world."),
                selected_music=MusicChoice("Bed", local_path=music),
            )
            bundle = _prepare_export_bundle(project)
            self.assertTrue(bundle.joined.is_file())
            self.assertTrue(bundle.source.is_file())
            self.assertTrue(bundle.music and bundle.music.is_file())
            self.assertTrue(bundle.verse_path and bundle.verse_path.is_file())
            self.assertIn("John 3:16", bundle.verse_path.read_text(encoding="utf-8"))


class TestGenerateWfpFromTemplate(unittest.TestCase):
    def test_sermon_highlights_export_uses_tl_source_trim(self) -> None:
        template_wfp = (
            Path(__file__).resolve().parents[1]
            / "assets"
            / "filmora_templates"
            / "sermon-highlights.wfp"
        )
        if not template_wfp.is_file():
            self.skipTest("sermon-highlights template missing")

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            joined = root / "joined.mp4"
            joined.write_bytes(b"\x00" * 128)
            sermon = root / "sermon.mp4"
            sermon.write_bytes(b"\x01" * 128)
            from unittest.mock import patch

            with patch("services.filmora_14_wfp.template_path", return_value=template_wfp):
                with patch(
                    "services.filmora_14_wfp._safe_duration",
                    side_effect=lambda path, default, **_: 3600.0
                    if "sermon" in path.name
                    else 275.0,
                ):
                    project = ProjectState(
                        project_name="trim-style-test",
                        sermon_path=sermon,
                        joined_clip_path=joined,
                        segments=[
                            ClipSegment("00:01:00", "00:03:40"),
                            ClipSegment("00:10:00", "00:14:18"),
                        ],
                    )
                    out = generate_wfp_from_template(project, output_path=root / "out.wfp")

            with zipfile.ZipFile(out) as zf:
                project_info = json.loads(zf.read("ProjectFolder/project_info.json"))
                timeline_id = project_info["timeline_mediaId"]
                timeline = json.loads(
                    zf.read(f"ProjectFolder/Medias/{timeline_id}/timeline.wesproj")
                )
                video_clips = [
                    clip
                    for ti in timeline["timelineInfos"]
                    for track in ti["trackInfos"]
                    for clip in track.get("clipList") or []
                    if ".mp4" in str(clip.get("filename", "")).lower()
                ]
                self.assertTrue(video_clips)
                first_seg = video_clips[0]
                self.assertGreater(first_seg["inPoint"], 0)
                self.assertGreater(first_seg["outPoint"], first_seg["inPoint"])
                self.assertEqual(first_seg["tlBegin"], 0)
                self.assertGreater(first_seg["tlEnd"], 0)
                self.assertGreater(project_info["project_timeline_duration"], 0)

    def test_sermon_highlights_export_patches_verse_text_layer(self) -> None:
        template_wfp = (
            Path(__file__).resolve().parents[1]
            / "assets"
            / "filmora_templates"
            / "sermon-highlights.wfp"
        )
        if not template_wfp.is_file():
            self.skipTest("sermon-highlights template missing")

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            joined = root / "joined.mp4"
            joined.write_bytes(b"\x00" * 128)
            sermon = root / "sermon.mp4"
            sermon.write_bytes(b"\x01" * 128)
            from unittest.mock import patch

            with patch("services.filmora_14_wfp.template_path", return_value=template_wfp):
                with patch(
                    "services.filmora_14_wfp._safe_duration",
                    side_effect=lambda path, default, **_: 3600.0
                    if "sermon" in path.name
                    else 275.0,
                ):
                    project = ProjectState(
                        project_name="verse-layer-test",
                        sermon_path=sermon,
                        joined_clip_path=joined,
                        segments=[
                            ClipSegment("00:01:00", "00:03:40"),
                            ClipSegment("00:10:00", "00:14:18"),
                        ],
                        selected_verse=VerseChoice("John 3:16", "For God so loved the world."),
                    )
                    out = generate_wfp_from_template(project, output_path=root / "out.wfp")

            with zipfile.ZipFile(out) as zf:
                timeline_id = json.loads(zf.read("ProjectFolder/project_info.json"))[
                    "timeline_mediaId"
                ]
                timeline = json.loads(
                    zf.read(f"ProjectFolder/Medias/{timeline_id}/timeline.wesproj")
                )
                self.assertGreaterEqual(len(timeline["timelineInfos"]), 2)
                segment_clips = [
                    clip
                    for track in timeline["timelineInfos"][0]["trackInfos"]
                    for clip in track.get("clipList") or []
                    if ".mp4" in str(clip.get("filename", "")).lower()
                    and int(clip.get("inPoint") or 0) > 0
                ]
                self.assertGreaterEqual(len(segment_clips), 2)

                verse_clip = timeline["timelineInfos"][1]["trackInfos"][0]["clipList"][0]
                script_buf = str(verse_clip.get("scriptBuf") or "")
                text_match = re.search(r'"Text":"([^"]*)"', script_buf)
                self.assertIsNotNone(text_match)
                self.assertIn("John 3:16", text_match.group(1))
                self.assertIn("For God so loved the world.", text_match.group(1))
                inner_end = int(verse_clip.get("tlEnd") or 0)

                compound_clips = [
                    clip
                    for track in timeline["timelineInfos"][0]["trackInfos"]
                    for clip in track.get("clipList") or []
                    if int(clip.get("timelineId") or 0) == int(
                        timeline["timelineInfos"][1].get("timelineId") or 0
                    )
                ]
                self.assertTrue(compound_clips)
                for compound in compound_clips:
                    self.assertEqual(int(compound.get("tlEnd") or 0), inner_end)

                manifest = json.loads(
                    (root / "wordly_manifest.json").read_text(encoding="utf-8")
                )
                verse_path = Path(manifest["media"]["verse"])
                verse_text = verse_path.read_text(encoding="utf-8")
                self.assertIn("John 3:16", verse_text)
                self.assertIn("For God so loved the world.", verse_text)

    def test_generate_rewrites_stale_template_paths(self) -> None:
        template_wfp = Path(__file__).resolve().parents[1] / "assets" / "filmora_templates" / "filmora_14_2_9.wfp"
        if not template_wfp.is_file():
            self.skipTest("bundled Filmora template missing")

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            joined = root / "joined.mp4"
            joined.write_bytes(b"\x00" * 64)
            sermon = root / "sermon.mp4"
            sermon.write_bytes(b"\x00" * 64)
            music = root / "bed.mp3"
            music.write_bytes(b"\x00" * 64)

            from unittest.mock import patch

            with patch("services.filmora_14_wfp.template_path", return_value=template_wfp):
                project = ProjectState(
                    project_name="wordly-fix-test",
                    sermon_path=sermon,
                    joined_clip_path=joined,
                    segments=[
                        ClipSegment("01:20:13", "01:22:53"),
                        ClipSegment("01:29:46", "01:34:07"),
                        ClipSegment("02:13:38", "02:20:37"),
                    ],
                    selected_verse=VerseChoice("John 3:16", "For God so loved the world."),
                    selected_music=MusicChoice("Bed", local_path=music),
                )
                out = generate_wfp_from_template(project, output_path=root / "out.wfp")

            bundle_dir = root
            media_dir = bundle_dir / "media"
            self.assertTrue(out.is_file())
            self.assertEqual(out, root / "out.wfp")
            self.assertTrue((media_dir / "highlights_joined.mp4").is_file())
            self.assertTrue((media_dir / "source_sermon.mp4").is_file())
            self.assertTrue((media_dir / "instrumental.mp3").is_file())
            self.assertTrue((bundle_dir / "verse.txt").is_file())
            self.assertTrue((bundle_dir / "wordly_manifest.json").is_file())

            with zipfile.ZipFile(out) as zf:
                medias = json.loads(zf.read("ProjectFolder/Medias/medias_info.json"))
                project_info = json.loads(zf.read("ProjectFolder/project_info.json"))
                timeline_id = project_info["timeline_mediaId"]
                video_urls = [
                    str(item.get("download_url", ""))
                    for item in medias["media_items"].values()
                    if int(item.get("media_type", 0)) == 8
                ]
                self.assertTrue(
                    any("highlights_joined" in u for u in video_urls),
                    video_urls,
                )
                self.assertTrue(
                    any("source_sermon" in u for u in video_urls),
                    video_urls,
                )
                for url in video_urls:
                    self.assertNotIn("Facebook_1", url)
                    self.assertIn("/media/", url.replace("\\", "/"))

                timeline = zf.read(
                    f"ProjectFolder/Medias/{timeline_id}/timeline.wesproj"
                ).decode("utf-8")
                json.loads(timeline)
                self.assertNotIn("\\Users", timeline)
                self.assertNotIn("file:/file:", timeline)
                self.assertNotIn("file:///", timeline)
                self.assertIn("highlights_joined", timeline)
                micro = sum(
                    1
                    for a, b in __import__("re").findall(
                        r'"inPoint":(\d+),"outPoint":(\d+)', timeline
                    )
                    if int(b) - int(a) <= 1
                )
                self.assertEqual(micro, 0)

                bundled_joined = media_dir / "highlights_joined.mp4"
                video_md5s = [
                    item["src_md5"]
                    for item in medias["media_items"].values()
                    if int(item.get("media_type", 0)) == 8
                ]
                self.assertIn(_file_md5(bundled_joined), video_md5s)

                with zipfile.ZipFile(template_wfp) as template_zip:
                    template_info = json.loads(
                        template_zip.read("ProjectFolder/project_info.json")
                    )
                export_info = json.loads(zf.read("ProjectFolder/project_info.json"))
                self.assertNotEqual(
                    export_info.get("project_guid"),
                    template_info.get("project_guid"),
                )


if __name__ == "__main__":
    unittest.main()
