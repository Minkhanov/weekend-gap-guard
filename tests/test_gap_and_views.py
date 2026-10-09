"""Gap model, history replay without look-ahead, and parsing of real recorded API answers."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from conftest import FIXTURES, TOKEN

from gapguard.market import calendar as cal
from gapguard.models import Candle, TokenRef
from gapguard.risk.gap import HOUR_MS, GapParams, build_model, observe_weekends
from gapguard.views import HistoryBook, Series, dynamic_view, history_view, no_trade_24h

UTC = timezone.utc


def hourly(start: datetime, hours: int, price_fn) -> list[Candle]:
    out = []
    for i in range(hours):
        t = start + timedelta(hours=i)
        p = price_fn(t)
        ms = int(t.timestamp() * 1000)
        out.append(Candle(ms, p, p, p, p, ms + HOUR_MS - 1))
    return out


def stepped_price(t: datetime) -> float:
    """Flat 100 inside each week; +2 % every Monday open (a pure weekend gap), weekend trading at +1 %."""
    weeks = (t - datetime(2026, 8, 3, tzinfo=UTC)).days // 7
    base = 100 * (1.02 ** weeks)
    if t.astimezone(cal.ET).weekday() >= 5:            # Saturday / Sunday: drifted halfway
        return base * 1.01 * 1.0
    return base


START = datetime(2026, 8, 3, 13, 0, tzinfo=UTC)     # Monday


def test_observe_weekends_measures_gap_drift_residual():
    candles = hourly(START, 24 * 7 * 6, stepped_price)
    obs = observe_weekends(candles)
    assert len(obs) >= 4
    o = obs[1]
    assert o.gap == pytest.approx(0.02, rel=1e-6)
    assert o.drift == pytest.approx(0.01, rel=1e-3)
    assert o.residual == pytest.approx(1.02 / 1.01 - 1, rel=1e-3)


def test_build_model_uses_observed_max_after_enough_weekends():
    candles = hourly(START, 24 * 7 * 10, stepped_price)
    m = build_model("X", candles, params=GapParams(min_samples=3, fat_tail=1.0))
    assert m.n >= 6
    assert m.q99 >= m.max_abs_gap        # q99 is never below the largest gap actually observed


def test_empirical_gap_overrides_a_too_small_parametric_quantile():
    def price(t):                        # one huge Monday gap (+15 %) in an otherwise calm series
        weeks = (t - datetime(2026, 8, 3, tzinfo=UTC)).days // 7
        return 100.0 * (1.15 if weeks >= 5 else 1.0) * (1 + 0.001 * (t.day % 3))
    candles = hourly(START, 24 * 7 * 12, price)
    m = build_model("X", candles, params=GapParams(min_samples=6))
    assert m.max_abs_gap == pytest.approx(0.15, abs=0.003)
    assert m.q99 == pytest.approx(m.max_abs_gap)
    assert any("exceeds the parametric q99" in n for n in m.notes)


def test_build_model_without_history():
    m = build_model("X", [])
    assert m.q99 is None and m.sigma_daily is None


def test_series_last_at_has_no_look_ahead():
    candles = hourly(datetime(2026, 10, 9, 10, 0, tzinfo=UTC), 5, lambda t: float(t.hour))
    s = Series(TOKEN, candles)
    # candle 12:00–13:00 closes at 13:00: at 12:59 we must still see the 11:00–12:00 candle
    assert s.last_at(datetime(2026, 10, 9, 12, 59, tzinfo=UTC))[0] == 11.0
    assert s.last_at(datetime(2026, 10, 9, 13, 0, tzinfo=UTC))[0] == 12.0
    assert s.last_at(datetime(2026, 10, 9, 9, 0, tzinfo=UTC)) is None


def test_history_view_reference_and_siblings():
    ondo = TokenRef("56", "0xa", "XYZon", "XYZ", 1)
    bstock = TokenRef("56", "0xb", "XYZB", "XYZ", 3)
    candles = hourly(START, 24 * 7 * 6, stepped_price)
    book = HistoryBook({"XYZon": Series(ondo, candles), "XYZB": Series(bstock, candles)})
    sat = datetime(2026, 8, 29, 16, 0, tzinfo=UTC)
    v = history_view(book, "XYZB", sat)
    assert v.reference_at == cal.last_close(sat)
    assert v.drift == pytest.approx(0.01, rel=1e-3)
    assert "XYZon" in v.siblings and v.dispersion == pytest.approx(0.0)
    assert v.gap is not None and v.gap.q99 > 0
    # the gap model for this weekend must not have seen the following Monday
    assert all(o.reopen_at <= sat for o in v.gap.observations)


def test_real_recorded_dynamic_answers_parse():
    raw = {s: json.loads((FIXTURES / f"dynamic_{s}.json").read_text())["data"] for s in ("TSLAon", "TSLAx", "TSLAB")}
    at = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
    v = dynamic_view(TOKEN, raw["TSLAon"], at, at, None, {"TSLAx": raw["TSLAx"], "TSLAB": raw["TSLAB"]}, "fixture")
    assert v.price == pytest.approx(371.688571)
    assert v.issuer_limit_usd == 975000
    assert v.status.market_status == "premarket"
    assert "TSLAx" not in v.siblings          # 24 h change exactly 0 → stale, excluded
    assert v.siblings["TSLAB"] == pytest.approx(371.82)
    assert no_trade_24h(raw["TSLAx"]["tokenInfo"]) and not no_trade_24h(raw["TSLAB"]["tokenInfo"])


def test_stale_token_gets_24h_quote_age_and_bstock_borrows_stock_print():
    raw = {s: json.loads((FIXTURES / f"dynamic_{s}.json").read_text())["data"] for s in ("TSLAon", "TSLAx", "TSLAB")}
    at = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
    x = TokenRef("56", "0x8a", "TSLAx", "TSLA", 2)
    vx = dynamic_view(x, raw["TSLAx"], at, at, None, {"TSLAon": raw["TSLAon"]}, "fixture")
    assert vx.quote_age_s == pytest.approx(24 * 3600)
    b = TokenRef("56", "0x5b", "TSLAB", "TSLA", 3)
    vb = dynamic_view(b, raw["TSLAB"], at, at, None, {"TSLAon": raw["TSLAon"]}, "fixture")
    assert vb.stock_price == pytest.approx(372.103333)
    assert any("borrowed" in n for n in vb.notes)


def test_real_kline_fixture_gives_a_sane_model():
    body = json.loads((FIXTURES / "kline1h_TSLAon.json").read_text())
    candles = [Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), int(r[6]))
               for r in body["data"]["klineInfos"]]
    m = build_model("TSLA", candles)
    assert m.sigma_daily is not None and 0.001 < m.sigma_daily < 0.2
    assert m.q99 > m.q95 > 0
