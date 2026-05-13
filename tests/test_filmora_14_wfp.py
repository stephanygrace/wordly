from __future__ import annotations

import json
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from models.project import MusicChoice, ProjectState
from services.filmora_14_wfp import (
    TEMPLATE_SOURCE_VIDEO_ID,
    TEMPLATE_VIDEO_ID,
    _filmora_path_str,
    _patch_medias_info,
    _register_replacement,
    _replace_paths_in_text,
    generate_wfp_from_template,
)


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
            }
        }
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            joined = root / "joined.mp4"
            joined.write_bytes(b"\x00")
            sermon = root / "sermon.mp4"
            sermon.write_bytes(b"\x00")
            project = ProjectState(sermon_path=sermon, project_name="demo")
            replacements = _patch_medias_info(
                data,
                project,
                video_path=joined,
                source_video_path=sermon,
                audio_path=None,
                video_duration_us=60_000_000,
                source_duration_us=120_000_000,
                audio_duration_us=0,
            )
            self.assertNotEqual(
                data["media_items"][TEMPLATE_SOURCE_VIDEO_ID]["download_url"],
                old_source,
            )
            self.assertIn(old_source, replacements)
            self.assertIn("sermon.mp4", data["media_items"][TEMPLATE_SOURCE_VIDEO_ID]["download_url"])


class TestGenerateWfpFromTemplate(unittest.TestCase):
    def test_generate_rewrites_stale_template_paths(self) -> None:
        template_root = Path(__file__).resolve().parents[1] / "temp" / "wfp_build_1778666800"
        if not template_root.is_dir():
            self.skipTest("reference wfp extract not available")

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            joined = root / "joined.mp4"
            joined.write_bytes(b"\x00" * 64)
            sermon = root / "sermon.mp4"
            sermon.write_bytes(b"\x00" * 64)
            music = root / "bed.mp3"
            music.write_bytes(b"\x00" * 64)

            template_wfp = root / "template.wfp"
            with zipfile.ZipFile(template_wfp, "w") as zf:
                for path in template_root.rglob("*"):
                    if path.is_file():
                        zf.write(path, path.relative_to(template_root).as_posix())

            from unittest.mock import patch

            with patch("services.filmora_14_wfp.template_path", return_value=template_wfp):
                project = ProjectState(
                    project_name="wordly-fix-test",
                    sermon_path=sermon,
                    joined_clip_path=joined,
                    selected_music=MusicChoice("Bed", local_path=music),
                )
                out = generate_wfp_from_template(project, output_path=root / "out.wfp")

            with zipfile.ZipFile(out) as zf:
                medias = json.loads(zf.read("ProjectFolder/Medias/medias_info.json"))
                source = medias["media_items"][TEMPLATE_SOURCE_VIDEO_ID]["download_url"]
                self.assertNotIn("Facebook_1", source)
                self.assertNotIn("Any Video Converter", source)

                b58_media = json.loads(
                    zf.read(
                        "ProjectFolder/Medias/{B58CC5D9-D4EA-4091-9FA5-863110B420D0}/media.json"
                    )
                )
                self.assertNotIn("Facebook_1", b58_media["file_name"])
                self.assertIn("sermon.mp4", b58_media["file_name"])
                self.assertNotIn("joined.mp4", b58_media["file_name"])


if __name__ == "__main__":
    unittest.main()
