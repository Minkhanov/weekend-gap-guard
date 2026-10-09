"""US equity (NYSE) regular-session calendar: the clock the underlying stocks live on.

Regular session 09:30–16:00 America/New_York; early closes at 13:00. DST is handled by zoneinfo.
Holidays: NYSE Group calendar for 2026–2027 (press release of 23 Dec 2025). Outside that range
the calendar falls back to "weekends only" and says so (`covered()` is False).

The live Market Status endpoint is the primary source for the *current* session; this module is
the offline fallback and the reference for history (which Friday close / Monday open to compare).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = timezone.utc
OPEN_T = time(9, 30)
CLOSE_T = time(16, 0)
EARLY_CLOSE_T = time(13, 0)

HOLIDAYS: dict[date, str] = {
    date(2026, 1, 1): "New Year's Day", date(2026, 1, 19): "Martin Luther King Jr. Day",
    date(2026, 2, 16): "Washington's Birthday", date(2026, 4, 3): "Good Friday",
    date(2026, 5, 25): "Memorial Day", date(2026, 6, 19): "Juneteenth",
    date(2026, 7, 3): "Independence Day (observed)", date(2026, 9, 7): "Labor Day",
    date(2026, 11, 26): "Thanksgiving Day", date(2026, 12, 25): "Christmas Day",
    date(2027, 1, 1): "New Year's Day", date(2027, 1, 18): "Martin Luther King Jr. Day",
    date(2027, 2, 15): "Washington's Birthday", date(2027, 3, 26): "Good Friday",
    date(2027, 5, 31): "Memorial Day", date(2027, 6, 18): "Juneteenth (observed)",
    date(2027, 7, 5): "Independence Day (observed)", date(2027, 9, 6): "Labor Day",
    date(2027, 11, 25): "Thanksgiving Day", date(2027, 12, 24): "Christmas Day (observed)",
}
EARLY_CLOSES: set[date] = {date(2026, 11, 27), date(2026, 12, 24), date(2027, 11, 26)}
COVERED_YEARS = (2026, 2027)


def covered(d: date) -> bool:
    return COVERED_YEARS[0] <= d.year <= COVERED_YEARS[1]


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def session_open(d: date) -> datetime:
    return datetime.combine(d, OPEN_T, ET).astimezone(UTC)


def session_close(d: date) -> datetime:
    return datetime.combine(d, EARLY_CLOSE_T if d in EARLY_CLOSES else CLOSE_T, ET).astimezone(UTC)


def _et_date(ts: datetime) -> date:
    return ts.astimezone(ET).date()


def prev_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def is_regular_open(now: datetime) -> bool:
    d = _et_date(now)
    return is_trading_day(d) and session_open(d) <= now < session_close(d)


def last_close(now: datetime) -> datetime:
    """Most recent regular-session close at or before `now`."""
    d = _et_date(now)
    if is_trading_day(d) and now >= session_close(d):
        return session_close(d)
    return session_close(prev_trading_day(d))


def next_open(now: datetime) -> datetime:
    d = _et_date(now)
    if is_trading_day(d) and now < session_open(d):
        return session_open(d)
    return session_open(next_trading_day(d))


def next_close(now: datetime) -> datetime:
    d = _et_date(now)
    if is_trading_day(d) and now < session_close(d):
        return session_close(d)
    return session_close(next_trading_day(d))


@dataclass(frozen=True)
class Closure:
    """The period the underlying market is shut: from one regular close to the next open."""

    start: datetime            # regular close (UTC)
    end: datetime              # next regular open (UTC)
    skipped_days: int          # calendar days between the two sessions with no trading at all
    holidays: tuple[str, ...]

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600

    @property
    def kind(self) -> str:
        if self.skipped_days == 0:
            return "overnight"
        if self.skipped_days == 2 and not self.holidays:
            return "weekend"
        return "long weekend" if self.skipped_days >= 3 else "holiday"


def closure_after(close_dt: datetime) -> Closure:
    d0 = _et_date(close_dt)
    d1 = next_trading_day(d0)
    skipped = (d1 - d0).days - 1
    hol = tuple(HOLIDAYS[d0 + timedelta(days=i)] for i in range(1, (d1 - d0).days) if d0 + timedelta(days=i) in HOLIDAYS)
    return Closure(start=session_close(d0), end=session_open(d1), skipped_days=skipped, holidays=hol)


def current_or_next_closure(now: datetime) -> Closure:
    """If the market is shut now — the closure we are in; if it is open — the one after today's close."""
    if is_regular_open(now):
        return closure_after(next_close(now))
    return closure_after(last_close(now))


def upcoming_multi_day_closure(now: datetime) -> Closure:
    """The next closure that skips at least one whole day (weekend / holiday) — what this tool guards."""
    c = current_or_next_closure(now)
    guard = 0
    while c.skipped_days == 0 and guard < 10:
        c = closure_after(session_close(next_trading_day(_et_date(c.start))))
        guard += 1
    return c


def phase(now: datetime, *, pre_close_min: int = 60, pre_open_min: int = 120) -> str:
    """open | pre_close | closed | pre_open — relative to multi-day closures only.

    Weeknight closures (one evening to the next morning) count as `open` for this tool:
    weekday overnight gaps are ordinary risk, the weekend is the special case.
    """
    c = current_or_next_closure(now)
    if c.skipped_days == 0:
        return "open"
    if now < c.start:
        return "pre_close" if (c.start - now) <= timedelta(minutes=pre_close_min) else "open"
    if (c.end - now) <= timedelta(minutes=pre_open_min):
        return "pre_open"
    return "closed"


def weekend_closures(start: datetime, end: datetime) -> list[Closure]:
    """All multi-day closures whose reopening falls inside [start, end] (for gap history)."""
    out: list[Closure] = []
    d = _et_date(start)
    stop = _et_date(end)
    while d <= stop:
        if is_trading_day(d):
            c = closure_after(session_close(d))
            if c.skipped_days > 0 and start <= c.start and c.end <= end:
                out.append(c)
        d += timedelta(days=1)
    return out
