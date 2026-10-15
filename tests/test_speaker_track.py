from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from services import speaker_track as st
from services.speaker_track import Box


def _box(cx: float, cy: float = 0.5, h: float = 0.6, face: bool = True, face_cx: float | None = None) -> Box:
    w = 0.15
    return Box(cx - w / 2, cy - h / 2, w, h, face, face_cx if face_cx is not None else (cx if face else None))


class TestTracking(unittest.TestCase):
    def test_links_people_across_samples(self) -> None:
        # Preacher drifting right, a musician standing still on the left.
        samples = [[_box(0.40 + i * 0.01), _box(0.10, h=0.5, face=False)] for i in range(10)]
        tracks = st.build_tracks(samples)
        self.assertEqual(len(tracks), 2)
        self.assertEqual(sorted(len(t.boxes) for t in tracks), [10, 10])

    def test_auto_picks_prominent_facing_person(self) -> None:
        samples = [[_box(0.5, h=0.5, face=True), _box(0.12, h=0.64, face=False)] for _ in range(8)]
        chosen = st.choose_track(st.build_tracks(samples), len(samples))
        self.assertAlmostEqual(next(iter(chosen.boxes.values())).cx, 0.5)

    def test_pick_overrides_automatic_choice(self) -> None:
        samples = [[_box(0.5), _box(0.15)] for _ in range(8)]
        tracks = st.build_tracks(samples)
        chosen = st.choose_track(tracks, len(samples), pick=(1.0, 0.15, 0.5))
        self.assertAlmostEqual(next(iter(chosen.boxes.values())).cx, 0.15)

    def test_camera_path_fills_gaps_and_uses_face(self) -> None:
        track = st.Track({0: _box(0.3, face_cx=0.32), 4: _box(0.5, face_cx=0.52)})
        path = st.camera_path(track, 6)
        self.assertEqual(len(path), 6)
        xs = [p[0] for p in path]
        self.assertTrue(all(0.32 <= x <= 0.52 for x in xs))
        self.assertLess(xs[0], xs[-1])

    def test_no_person_stays_centred(self) -> None:
        self.assertEqual(st.camera_path(None, 3), [(0.5, 0.5, 1.0, 0.5, 1.0)] * 3)

    def test_upper_body_box_matched_to_person(self) -> None:
        uppers = [(0.80, 0.10, 0.1, 0.2), (0.20, 0.31, 0.1, 0.28)]
        self.assertEqual(st._matching_upper(0.18, 0.30, 0.15, 0.6, uppers), uppers[1])
        self.assertIsNone(st._matching_upper(0.5, 0.3, 0.1, 0.6, uppers))

    def test_waist_estimated_without_upper_body(self) -> None:
        box = Box(0.4, 0.2, 0.2, 0.6)
        self.assertAlmostEqual(box.waist_h, 0.3)
        self.assertAlmostEqual(box.waist_cy, 0.35)


class TestPortraitCrop(unittest.TestCase):
    def test_centres_person_with_full_height_window(self) -> None:
        w, h, x, y = st.portrait_crop(0.5, 0.5, 1280, 720)
        self.assertEqual((w, h), (404, 720))
        self.assertAlmostEqual(x + w / 2, 640, delta=2)

    def test_zooms_in_near_edge_to_stay_centred(self) -> None:
        # Person at 10% from the left: a full-height window would hit the edge.
        w, h, x, y = st.portrait_crop(0.10, 0.5, 1280, 720)
        self.assertLess(w, 404)
        self.assertAlmostEqual(x + w / 2, 128, delta=2)
        self.assertAlmostEqual(w / h, 9 / 16, delta=0.01)

    def test_frames_whole_body_head_to_feet(self) -> None:
        # Person 60% of the frame tall, centred at (0.4, 0.55).
        w, h, x, y = st.portrait_crop(0.4, 0.55, 1280, 720, 0.6)
        body_top, body_bottom = (0.55 - 0.3) * 720, (0.55 + 0.3) * 720
        self.assertLessEqual(y, body_top)
        self.assertGreaterEqual(y + h, body_bottom)
        # Only a small margin around the body: zoomed in, not the full 720px height.
        self.assertAlmostEqual(h, 0.6 * 720 * (1 + 2 * st.BODY_MARGIN), delta=4)
        self.assertAlmostEqual(x + w / 2, 512, delta=2)
        self.assertAlmostEqual(y + h / 2, 0.55 * 720, delta=2)

    def test_zoom_smoothing_is_slower_than_panning(self) -> None:
        # Arms raised for one sample: the body box is briefly much taller.
        boxes = {i: _box(0.5, h=0.5) for i in range(12)}
        boxes[6] = _box(0.5, h=0.9)
        path = st.camera_path(st.Track(boxes), 12)
        self.assertLess(path[6][2], 0.6)

    def test_zoom_has_a_limit(self) -> None:
        w, h, x, y = st.portrait_crop(0.0, 0.5, 1280, 720)
        self.assertEqual(x, 0)
        self.assertGreaterEqual(w, 200)

    def test_background_crop_is_steady_and_keeps_speaker_in_frame(self) -> None:
        # Speaker sways a little, then walks right across the stage.
        xs = [0.40 + 0.01 * ((i % 4) - 2) for i in range(20)] + [0.40 + 0.02 * i for i in range(20)]
        camera = [(x, 0.5, 0.6) for x in xs]
        rects = st.crop_rects(camera, src_w=1280, src_h=720, frames=300, out_fps=30)
        self.assertEqual(len(rects), 300)
        self.assertEqual(len({(w, h, y) for w, h, _, y in rects}), 1)  # fixed zoom and height
        swaying = {x for _, _, x, _ in rects[: 4 * 30]}
        self.assertLessEqual(max(swaying) - min(swaying), 2)  # holds still while swaying
        for n, (w, h, x, y) in enumerate(rects):
            subject = st._at([c[0] * 1280 for c in camera], n / 30)
            self.assertTrue(x <= subject <= x + w)


class TestMainCrop(unittest.TestCase):
    def test_frames_head_to_waist_at_constant_zoom(self) -> None:
        # Head-to-waist is 30% of the frame height in most samples; one outlier.
        camera = [(0.3, 0.5, 0.6, 0.4, 0.30)] * 9 + [(0.3, 0.5, 0.6, 0.4, 0.9)]
        w, h = st.main_crop_size(camera, 1280, 720)
        self.assertAlmostEqual(h, 0.30 * 720 * (1 + 2 * st.UPPER_MARGIN), delta=4)
        self.assertAlmostEqual(w / h, 16 / 9, delta=0.02)

    def test_zoom_is_limited_for_far_speakers(self) -> None:
        w, h = st.main_crop_size([(0.5, 0.5, 0.2, 0.5, 0.05)], 1280, 720, max_zoom=3.0)
        self.assertAlmostEqual(h, 240, delta=2)

    def test_positions_steady_height_and_speaker_in_view(self) -> None:
        camera = [(0.30 + 0.004 * i, 0.5, 0.6, 0.40, 0.3) for i in range(40)]
        size = st.main_crop_size(camera, 1280, 720)
        positions = st.main_crop_positions(camera, size=size, src_w=1280, src_h=720, frames=300)
        self.assertEqual(len(positions), 300)
        self.assertEqual(len({y for _, y in positions}), 1)
        self.assertAlmostEqual(positions[0][1] + size[1] / 2, 0.40 * 720, delta=2)
        for n, (x, _) in enumerate(positions):
            subject = st._at([c[0] * 1280 for c in camera], n / 30)
            self.assertTrue(x <= subject <= x + size[0])
        edge = st.main_crop_positions([(0.02, 0.5, 0.6, 0.05, 0.3)], size=size, src_w=1280, src_h=720, frames=1)
        self.assertEqual(edge, [(0, 0)])


class TestLazyFollow(unittest.TestCase):
    def test_holds_still_inside_dead_zone(self) -> None:
        subject = [500 + (20 if i % 2 else -20) for i in range(30)]
        centres = st.lazy_follow(subject, 400, lo=0, hi=2000)
        self.assertEqual(len(set(round(c) for c in centres)), 1)

    def test_follows_a_walk_smoothly_without_jumps(self) -> None:
        subject = [500.0] * 10 + [500.0 + 30 * i for i in range(1, 21)] + [1100.0] * 10
        centres = st.lazy_follow(subject, 400, lo=0, hi=2000)
        zone = st.DEAD_ZONE * 400
        self.assertLessEqual(abs(centres[-1] - 1100), zone + 1)  # caught up, subject inside comfort zone
        steps = [abs(b - a) for a, b in zip(centres, centres[1:])]
        self.assertLess(max(steps), 40)  # glides, no snapping

    def test_never_leaves_source_frame(self) -> None:
        centres = st.lazy_follow([5.0] * 10, 400, lo=200, hi=1080)
        self.assertTrue(all(c == 200 for c in centres))

class TestSampling(unittest.TestCase):
    def test_samples_frames_at_requested_rate(self) -> None:
        import subprocess

        with TemporaryDirectory() as tmp:
            clip = Path(tmp) / "c.mp4"
            subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=s=320x180:d=2:r=30", str(clip)],
                check=True,
            )
            seen: list[Path] = []
            samples = st.sample_detections(clip, fps=4, detector=lambda p: seen.append(p) or [_box(0.5)])
        self.assertEqual(len(samples), 8)
        self.assertEqual(len(seen), 8)


if __name__ == "__main__":
    unittest.main()
