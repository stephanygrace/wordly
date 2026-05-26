from __future__ import annotations

import os
import unittest
from unittest import mock

from utils.subprocess_win import background_creationflags


class TestSubprocessWin(unittest.TestCase):
    def test_background_creationflags_zero_off_windows(self) -> None:
        with mock.patch.object(os, "name", "posix"):
            self.assertEqual(background_creationflags(), 0)

    def test_background_creationflags_uses_detached_on_windows(self) -> None:
        import subprocess

        with mock.patch.object(os, "name", "nt"):
            flags = background_creationflags()
        expected = 0
        if hasattr(subprocess, "DETACHED_PROCESS"):
            expected |= subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            expected |= subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        self.assertEqual(flags, expected)
        self.assertNotEqual(flags, 0)


if __name__ == "__main__":
    unittest.main()
