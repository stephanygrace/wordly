from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from services import screen_gaze as sg
from services.screen_gaze import Face


def _speaker(yaw: float, cx: float = 0.22) -> Face:
    return Face(cx, 0.03, yaw)


class TestSpeakerYaws(unittest.TestCase):
    def test_follows_the_usual_face_not_a_passer_by(self) -> None:
        # Preacher at the pulpit; someone close to the camera (a bigger face) walks through.
        faces = {t: [_speaker(0.05)] for t in range(20)}
        for t in range(5, 9):
            faces[t] = [Face(0.06, 0.06, 1.4)]
        yaws = sg.speaker_yaws(faces, list(range(20)))
        self.assertEqual(yaws[0], 0.05)
        self.assertTrue(all(yaws[t] is None for t in range(5, 9)))  # passer-by ignored

    def test_finds_the_speaker_again_after_they_move_out_of_sight(self) -> None:
        faces = {t: [_speaker(0.0, cx=0.22)] for t in range(10)}
        faces.update({t: [] for t in range(10, 20)})
        faces.update({t: [_speaker(0.1, cx=0.6)] for t in range(20, 26)})
        yaws = sg.speaker_yaws(faces, list(range(26)))
        self.assertEqual(yaws[25], 0.1)


class TestReadingStretches(unittest.TestCase):
    def test_sustained_turn_to_the_screen_is_reading(self) -> None:
        yaws = {t: 0.1 for t in range(60)}
        yaws.update({t: (None if t % 3 else -0.5) for t in range(20, 40)})  # turned away or face lost
        self.assertEqual(sg.reading_stretches(yaws), [(20, 40)])

    def test_glancing_around_the_room_is_not_reading(self) -> None:
        # Looking left, right and at the camera in turn, with the odd lost face.
        pattern = [-0.4, 0.0, 0.3, None, 0.1]
        yaws = {t: pattern[t % len(pattern)] for t in range(120)}
        self.assertEqual(sg.reading_stretches(yaws), [])

    def test_windows_do_not_bridge_separate_scan_ranges(self) -> None:
        yaws = {t: None for t in list(range(0, 10)) + list(range(100, 110))}
        self.assertEqual(sg.reading_stretches(yaws), [])


class TestSampleFaces(unittest.TestCase):
    def test_samples_once_a_second_and_caches_each_chunk(self) -> None:
        runs: list[list[str]] = []

        def fake_ffmpeg(cmd, **_kw):
            runs.append(cmd)
            for n in range(1, 4):
                (Path(cmd[-1]).parent / f"f_{n:04d}.jpg").write_bytes(b"jpg")
            return SimpleNamespace(returncode=0)

        with TemporaryDirectory() as tmp:
            video = Path(tmp) / "service.mp4"
            video.write_bytes(b"x")
            with mock.patch.object(sg, "GAZE_CACHE", Path(tmp) / "gaze"), mock.patch.object(
                sg.subprocess, "run", side_effect=fake_ffmpeg
            ):
                faces = sg.sample_faces(video, [(1200.0, 1210.0)], detector=lambda _f: [_speaker(-0.3)])
                self.assertEqual(sorted(faces), [1200, 1201, 1202])
                self.assertEqual(faces[1201], [_speaker(-0.3)])
                self.assertEqual(runs[0][runs[0].index("-ss") + 1], "1200")
                sg.sample_faces(video, [(1200.0, 1210.0)], detector=lambda _f: [])
                self.assertEqual(len(runs), 1)  # second call read the cache


if __name__ == "__main__":
    unittest.main()
