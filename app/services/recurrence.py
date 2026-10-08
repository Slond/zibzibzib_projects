"""Calendar recurrence with a stable day-of-month anchor."""
import calendar
from datetime import timedelta


def advance_run(base, frequency, value=1, anchor_day=None):
    if frequency == "monthly":
        year = base.year + (base.month == 12)
        month = base.month % 12 + 1
        return base.replace(year=year, month=month, day=min(anchor_day or base.day, calendar.monthrange(year, month)[1]))
    if frequency == "daily":
        return base + timedelta(days=1)
    if frequency == "weekly":
        return base + timedelta(weeks=1)
    if frequency == "every_n_days" and 1 <= value <= 3650:
        return base + timedelta(days=value)
    raise ValueError("Invalid recurrence frequency")
