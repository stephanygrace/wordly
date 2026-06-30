from __future__ import annotations

from datetime import date, timedelta


def last_sunday(from_date: date | None = None) -> date:
    """Return the most recent Sunday on or before *from_date* (local calendar)."""
    d = from_date or date.today()
    days_since_sunday = (d.weekday() + 1) % 7
    return d - timedelta(days=days_since_sunday)


def default_export_project_name(from_date: date | None = None) -> str:
    """Default Filmora export name: last Sunday as MM.DD.YY (e.g. 06.28.26)."""
    return last_sunday(from_date).strftime("%m.%d.%y")
