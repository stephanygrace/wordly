import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from models.project import MusicChoice, ProjectState, VerseChoice
from services.ai_assistant import suggest_bible_verses, suggest_instrumentals
from services.resolve_export import generate_resolve_project


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


class TestResolvePipeline(unittest.TestCase):
    def test_generate_resolve_project_smoke(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            clip = root / "joined.mp4"
            clip.write_bytes(b"\x00" * 128)
            music = root / "bed.mp3"
            music.write_bytes(b"\x00" * 128)

            project = ProjectState(
                reel_layout=False,
                project_name="test-project",
                joined_clip_path=clip,
                selected_verse=VerseChoice("John 3:16", "For God so loved the world."),
                selected_music=MusicChoice("Calm Piano", local_path=music),
                theme="love",
            )

            with patch("services.resolve_export._safe_duration", return_value=3.0):
                with patch("services.resolve_export.subprocess.run"):
                    out = generate_resolve_project(project, output_dir=root / "out")
            self.assertTrue(out.exists())
            self.assertEqual(out.suffix, ".fcpxml")
            self.assertTrue((root / "out" / "media" / "Clip001.mp4").exists())


if __name__ == "__main__":
    unittest.main()
