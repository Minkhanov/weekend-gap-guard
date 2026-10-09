from datetime import date, datetime, timezone

from conftest import FRI_OPEN, FRI_PRE_CLOSE, MON_PRE_OPEN, SAT_NOON, TUE_NIGHT, XMAS_SAT

from gapguard.market import calendar as cal

UTC = timezone.utc


def test_phases_around_a_normal_weekend():
    assert cal.phase(FRI_OPEN) == "open"
    assert cal.phase(FRI_PRE_CLOSE) == "pre_close"
    assert cal.phase(SAT_NOON) == "closed"
    assert cal.phase(MON_PRE_OPEN) == "pre_open"


def test_weekday_overnight_is_not_a_multi_day_closure():
    assert cal.phase(TUE_NIGHT) == "open"
    assert not cal.is_regular_open(TUE_NIGHT)
    assert cal.current_or_next_closure(TUE_NIGHT).kind == "overnight"


def test_weekend_closure_bounds_in_utc_with_dst():
    c = cal.current_or_next_closure(SAT_NOON)
    assert c.start == datetime(2026, 10, 9, 20, 0, tzinfo=UTC)      # 16:00 EDT
    assert c.end == datetime(2026, 10, 12, 13, 30, tzinfo=UTC)      # 09:30 EDT
    assert c.kind == "weekend" and c.skipped_days == 2
    assert round(c.hours, 1) == 65.5


def test_christmas_long_weekend_with_early_close():
    c = cal.current_or_next_closure(XMAS_SAT)
    assert c.start == datetime(2026, 12, 24, 18, 0, tzinfo=UTC)     # 13:00 EST early close
    assert c.end == datetime(2026, 12, 28, 14, 30, tzinfo=UTC)
    assert c.kind == "long weekend" and c.skipped_days == 3
    assert "Christmas Day" in c.holidays


def test_thanksgiving_midweek_holiday_counts_as_closure():
    wed_night = datetime(2026, 11, 26, 3, 0, tzinfo=UTC)
    c = cal.current_or_next_closure(wed_night)
    assert c.kind == "holiday" and c.skipped_days == 1
    assert cal.phase(wed_night) == "closed"


def test_trading_day_helpers():
    assert not cal.is_trading_day(date(2026, 10, 10))            # Saturday
    assert not cal.is_trading_day(date(2026, 11, 26))            # Thanksgiving
    assert cal.is_trading_day(date(2026, 10, 12))                # Columbus Day: NYSE is open
    assert cal.prev_trading_day(date(2026, 10, 12)) == date(2026, 10, 9)
    assert cal.last_close(SAT_NOON) == datetime(2026, 10, 9, 20, 0, tzinfo=UTC)
    assert cal.next_open(SAT_NOON) == datetime(2026, 10, 12, 13, 30, tzinfo=UTC)


def test_weekend_closures_listing():
    cs = cal.weekend_closures(datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 13, tzinfo=UTC))
    starts = [c.start.date().isoformat() for c in cs]
    assert "2026-10-09" in starts and "2026-09-04" in starts
    labor_day = next(c for c in cs if c.start.date() == date(2026, 9, 4))
    assert labor_day.kind == "long weekend"
