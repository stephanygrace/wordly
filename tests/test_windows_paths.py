import unittest

from utils.windows_paths import filmora_media_path


class TestWindowsPaths(unittest.TestCase):
    def test_mnt_c_mapping(self) -> None:
        path = filmora_media_path(__import__("pathlib").Path("/mnt/c/Users/demo/video.mp4"))
        self.assertEqual(path, "C:\\Users\\demo\\video.mp4")

    def test_wsl_home_unc(self) -> None:
        import os
        from pathlib import Path

        old = os.environ.get("WSL_DISTRO_NAME")
        os.environ["WSL_DISTRO_NAME"] = "Ubuntu"
        try:
            path = filmora_media_path(Path("/home/stelle/Projects/wordly/clips/joined.mp4"))
            self.assertTrue(path.startswith("\\\\wsl$\\Ubuntu\\home\\stelle\\"))
        finally:
            if old is None:
                os.environ.pop("WSL_DISTRO_NAME", None)
            else:
                os.environ["WSL_DISTRO_NAME"] = old


if __name__ == "__main__":
    unittest.main()
