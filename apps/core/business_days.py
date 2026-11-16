"""Business-day maths for deadlines (unit holds, partner confirmation SLA): weekends and public holidays are skipped."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from django.conf import settings
from django.utils import timezone


def holidays() -> set[date]:
    out = set()
    for d in settings.NEBRET.get("HOLIDAYS", []):
        try:
            out.add(date.fromisoformat(str(d)))
        except ValueError:
            continue
    return out


def is_business_day(d: date, _holidays: set[date] | None = None) -> bool:
    return d.weekday() < 5 and d not in (holidays() if _holidays is None else _holidays)


def add_business_days(start: datetime, days: int) -> datetime:
    """``start`` + ``days`` business days, keeping the time of day (a Friday 15:00 + 1 → Monday 15:00)."""
    hol = holidays()
    current = start
    left = days
    while left > 0:
        current += timedelta(days=1)
        if is_business_day(timezone.localtime(current).date() if timezone.is_aware(current) else current.date(), hol):
            left -= 1
    return current


def business_days_between(start: datetime, end: datetime) -> int:
    """Whole business days that have passed from ``start`` to ``end``."""
    if end <= start:
        return 0
    n, probe = 0, start
    while True:
        nxt = add_business_days(probe, 1)
        if nxt > end:
            return n
        n, probe = n + 1, nxt
