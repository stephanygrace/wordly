import json
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from models.project import ClipSegment, MusicChoice, ProjectState, VerseChoice
from services.ai_assistant import suggest_bible_verses, suggest_instrumentals
from services.wfp_generator import build_layers, generate_wfp


class TestAiAssistant(unittest.TestCase):
    def test_suggest_bible_verses_offline(self) -> None:
        verses = suggest_bible_verses("hope and peace")
        self.assertGreaterEqual(len(verses), 1)
        self.assertTrue(verses[0].reference)
        self.assertTrue(verses[0].text)

    def test_suggest_instrumentals_offline(self) -> None:
        songs = suggest_instrumentals("grace")
        self.assertGreaterEqual(len(songs), 1)
        self.assertTrue(songs[0].search_query)


class TestWfpGenerator(unittest.TestCase):
    def test_generate_wfp_creates_zip_layers(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            joined = root / "joined.mp4"
            joined.write_bytes(b"\x00" * 128)
            music = root / "bed.mp3"
            music.write_bytes(b"\x00" * 128)

            project = ProjectState(
                project_name="test-project",
                joined_clip_path=joined,
                selected_verse=VerseChoice("John 3:16", "For God so loved the world."),
                selected_music=MusicChoice("Calm Piano", local_path=music),
                theme="love",
            )

            # ffprobe won't work on dummy bytes; stub layers directly for zip structure test.
            layers = build_layers(project)
            self.assertEqual(len(layers), 3)

            out = generate_wfp(project, output_path=root / "demo.wfp")
            self.assertTrue(out.exists())
            with zipfile.ZipFile(out) as zf:
                names = zf.namelist()
                self.assertIn("ProjectFolder/project_info.json", names)
                self.assertIn("ProjectFolder/Medias/medias_info.json", names)
                project_info = json.loads(zf.read("ProjectFolder/project_info.json"))
                self.assertEqual(project_info.get("project_editor_create_version"), "14.2.9.11061")


if __name__ == "__main__":
    unittest.main()
