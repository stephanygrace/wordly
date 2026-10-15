from __future__ import annotations

import os
import time
import unittest


class TestBackToBackJobs(unittest.TestCase):
    """Regression: starting a job the moment the previous one finished crashed the app."""

    def test_many_jobs_back_to_back_then_close(self) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        from ui.wizard_window import WizardWindow

        window = WizardWindow()
        done: list[int] = []

        def job(*, progress_cb, should_cancel):
            time.sleep(0.01)
            return "ok"

        for i in range(60):
            window._run_job(job, on_ok=lambda _r, i=i: done.append(i))
            deadline = time.time() + 10
            while len(done) <= i and time.time() < deadline:
                app.processEvents()
        window.close()
        app.processEvents()
        self.assertEqual(len(done), 60)
        self.assertEqual(window._retired_jobs, [])


if __name__ == "__main__":
    unittest.main()
