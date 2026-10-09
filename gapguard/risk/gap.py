"""Weekend gap model for one underlying ticker, built from the token's own hourly candles.

Definitions (all prices per token, from 1h candles aligned to UTC hours):
  friday_close  — close of the candle ending at the regular close (16:00 ET, 13:00 on early closes)
  weekend_last  — close of the last candle ending at or before Sunday 20:00 ET (= reopen − 13.5 h),
                  i.e. the weekend price just before any Sunday-evening session can start
  monday_ref    — close of the candle ending 30 min after the regular open (10:00 ET): the price
                  once the underlying market has re-anchored the token
  gap      = monday_ref / friday_close − 1     (what a holder over the whole weekend absorbs)
  drift    = weekend_last / friday_close − 1   (how far weekend on-chain trading already moved)
  residual = monday_ref / weekend_last − 1     (what was still left for Monday)

Risk estimate (fractions, not %):
  sigma_daily — st.dev. of log returns between consecutive regular closes (same candle series)
  sigma_gap   — sigma_daily × sqrt(weekend_variance_factor + 0.25 × extra skipped days)
                × earnings multiplier when an earnings event is flagged
  q99_param   — 2.576 × sigma_gap × fat_tail
  q99         — max(q99_param, largest observed |gap|) once ≥ min_samples weekends are observed
Assumptions are explicit parameters (GapParams) and are printed with every report.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..market import calendar as cal
from ..models import Candle

HOUR_MS = 3_600_000
Z99 = 2.5758
Z95 = 1.6449


@dataclass(frozen=True)
class GapParams:
    weekend_variance_factor: float = 1.0   # weekend close→open variance in units of one trading day
    extra_day_factor: float = 0.25         # added per skipped day beyond a normal weekend (holidays)
    fat_tail: float = 1.25                 # widen the normal quantile for fat tails
    earnings_multiplier: float = 2.5       # earnings inside the closure (or ASSET_LIMITED: earnings)
    min_samples: int = 6                   # observed weekends needed before the empirical max counts
    ffill_hours: int = 6                   # sparse markets: accept the last candle up to N hours old


@dataclass(frozen=True)
class GapObservation:
    close_at: datetime
    reopen_at: datetime
    friday_close: float
    weekend_last: float | None
    monday_ref: float
    kind: str

    @property
    def gap(self) -> float:
        return self.monday_ref / self.friday_close - 1

    @property
    def drift(self) -> float | None:
        return None if self.weekend_last is None else self.weekend_last / self.friday_close - 1

    @property
    def residual(self) -> float | None:
        return None if self.weekend_last is None else self.monday_ref / self.weekend_last - 1


@dataclass
class GapModel:
    ticker: str
    sigma_daily: float | None
    sigma_gap: float | None
    q95: float | None
    q99: float | None
    q99_param: float | None
    observations: list[GapObservation] = field(default_factory=list)
    returns_used: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.observations)

    @property
    def max_abs_gap(self) -> float | None:
        return max((abs(o.gap) for o in self.observations), default=None)

    @property
    def mean_abs_gap(self) -> float | None:
        return statistics.fmean(abs(o.gap) for o in self.observations) if self.observations else None

    @property
    def priced_in_share(self) -> float | None:
        """1 − mean|residual| / mean|gap|: how much of the Monday move weekend trading had already shown."""
        pairs = [(abs(o.gap), abs(o.residual)) for o in self.observations if o.residual is not None]
        if len(pairs) < 3:
            return None
        mg = statistics.fmean(p[0] for p in pairs)
        mr = statistics.fmean(p[1] for p in pairs)
        return None if mg == 0 else 1 - mr / mg


def index_by_close(candles: list[Candle]) -> dict[int, Candle]:
    return {c.open_ms + HOUR_MS: c for c in candles}


def price_at(candles: list[Candle], by_close: dict[int, Candle], when: datetime, ffill_hours: int) -> float | None:
    """Close of the 1h candle ending exactly at `when`; else the latest candle ending within ffill window."""
    t = int(when.timestamp() * 1000)
    c = by_close.get(t)
    if c is not None:
        return c.close
    floor = t - ffill_hours * HOUR_MS
    best: Candle | None = None
    for cand in candles:                     # candles are sorted by open time
        end = cand.open_ms + HOUR_MS
        if end > t:
            break
        if end >= floor:
            best = cand
    return best.close if best else None


def observe_weekends(candles: list[Candle], params: GapParams = GapParams()) -> list[GapObservation]:
    if not candles:
        return []
    start = datetime.fromtimestamp(candles[0].open_ms / 1000, tz=cal.UTC)
    end = datetime.fromtimestamp((candles[-1].open_ms + HOUR_MS) / 1000, tz=cal.UTC)
    by_close = index_by_close(candles)
    out = []
    for c in cal.weekend_closures(start, end):
        fri = price_at(candles, by_close, c.start, params.ffill_hours)
        mon_t = c.end + timedelta(minutes=30)
        if mon_t > end:
            continue
        mon = price_at(candles, by_close, mon_t, params.ffill_hours)
        wk_t = (c.end - timedelta(hours=13, minutes=30)).replace(minute=0, second=0, microsecond=0)
        wk = price_at(candles, by_close, wk_t, params.ffill_hours) if wk_t > c.start else None
        if fri and mon:
            out.append(GapObservation(c.start, c.end, fri, wk, mon, c.kind))
    return out


def close_to_close_returns(candles: list[Candle], params: GapParams = GapParams()) -> list[float]:
    if not candles:
        return []
    by_close = index_by_close(candles)
    start = datetime.fromtimestamp(candles[0].open_ms / 1000, tz=cal.UTC)
    end = datetime.fromtimestamp((candles[-1].open_ms + HOUR_MS) / 1000, tz=cal.UTC)
    closes: list[float] = []
    d = start.astimezone(cal.ET).date()
    while d <= end.astimezone(cal.ET).date():
        if cal.is_trading_day(d):
            t = cal.session_close(d)
            if start <= t <= end:
                p = price_at(candles, by_close, t, params.ffill_hours)
                if p:
                    closes.append(p)
        d += timedelta(days=1)
    return [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]


def build_model(ticker: str, candles: list[Candle], *, closure: cal.Closure | None = None,
                earnings: bool = False, params: GapParams = GapParams()) -> GapModel:
    rets = close_to_close_returns(candles, params)
    obs = observe_weekends(candles, params)
    notes: list[str] = []
    if len(rets) < 10:
        notes.append(f"only {len(rets)} daily returns — volatility estimate is weak")
        sigma_daily = statistics.pstdev(rets) if len(rets) >= 2 else None
    else:
        sigma_daily = statistics.stdev(rets)
    if sigma_daily is None:
        return GapModel(ticker, None, None, None, None, None, obs, len(rets), notes + ["no usable price history"])

    factor = params.weekend_variance_factor
    if closure is not None and closure.skipped_days > 2:
        factor += params.extra_day_factor * (closure.skipped_days - 2)
        notes.append(f"{closure.kind}: {closure.skipped_days} days without a session ({', '.join(closure.holidays) or 'holiday'})")
    sigma_gap = sigma_daily * math.sqrt(factor)
    if earnings:
        sigma_gap *= params.earnings_multiplier
        notes.append(f"earnings flagged: gap sigma ×{params.earnings_multiplier}")
    q99_param = Z99 * sigma_gap * params.fat_tail
    q95 = Z95 * sigma_gap * params.fat_tail
    q99 = q99_param
    if len(obs) >= params.min_samples:
        emp = max(abs(o.gap) for o in obs)
        if emp > q99:
            notes.append(f"observed weekend gap {emp:.2%} exceeds the parametric q99 — using the observed one")
            q99 = emp
    else:
        notes.append(f"{len(obs)} weekends observed (< {params.min_samples}) — parametric estimate only")
    return GapModel(ticker, sigma_daily, sigma_gap, q95, q99, q99_param, obs, len(rets), notes)
