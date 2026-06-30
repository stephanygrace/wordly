from __future__ import annotations

import unittest
from datetime import date

from utils.export_name import default_export_project_name, last_sunday


class TestExportName(unittest.TestCase):
    def test_last_sunday_on_tuesday(self) -> None:
        # Tuesday, June 30, 2026 → previous Sunday was June 28.
        tuesday = date(2026, 6, 30)
        self.assertEqual(last_sunday(tuesday), date(2026, 6, 28))
        self.assertEqual(default_export_project_name(tuesday), "06.28.26")

    def test_last_sunday_on_sunday(self) -> None:
        sunday = date(2026, 6, 28)
        self.assertEqual(last_sunday(sunday), sunday)
        self.assertEqual(default_export_project_name(sunday), "06.28.26")

    def test_last_sunday_on_monday(self) -> None:
        monday = date(2026, 6, 29)
        self.assertEqual(last_sunday(monday), date(2026, 6, 28))
        self.assertEqual(default_export_project_name(monday), "06.28.26")


if __name__ == "__main__":
    unittest.main()
