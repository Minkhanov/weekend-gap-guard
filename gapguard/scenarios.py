"""Replay real weekends hour by hour, and measure the gap model against what Monday actually did.

Everything is computed from recorded hourly candles with no look-ahead: the decision at hour t sees
only candles that closed by t, and the gap model for a weekend is fitted on data before Friday's close.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from .market import calendar as cal
from .risk.account import AccountState, demo_account
from .risk.engine import OrderIntent, evaluate
from .risk.gap import observe_weekends
from .risk.policy import GuardPolicy
from .views import REPLAY_QUOTE_AGE_S, HistoryBook, history_view

UTC = timezone.utc
REPLAY_POLICY = GuardPolicy(max_quote_age_s=REPLAY_QUOTE_AGE_S)


def weekends(book: HistoryBook, ticker: str) -> list[cal.Closure]:
    a = book.anchor(ticker)
    if a is None or not a.candles:
        return []
    start = datetime.fromtimestamp(a.candles[0].open_ms / 1000, tz=UTC)
    end = datetime.fromtimestamp(a.candles[-1].open_ms / 1000 + 3600, tz=UTC)
    return [c for c in cal.weekend_closures(start, end) if c.end + timedelta(minutes=30) <= end]


def find_weekend(book: HistoryBook, ticker: str, friday: str) -> cal.Closure:
    """`friday` is the ET date of the close, e.g. 2026-10-02."""
    for c in weekends(book, ticker):
        if c.start.astimezone(cal.ET).date().isoformat() == friday:
            return c
    raise ValueError(f"no recorded weekend for {ticker} after {friday}")


def weekend_timeline(book: HistoryBook, ticker: str, closure: cal.Closure, *, order_usd: float = 2000.0,
                     account: AccountState | None = None, policy: GuardPolicy = REPLAY_POLICY,
                     step_min: int = 60) -> dict[str, Any]:
    account = account or demo_account()
    group = book.by_ticker.get(ticker) or {}
    symbols = sorted(group, key=lambda s: group[s].token.type_id)
    t0 = (closure.start - timedelta(hours=4)).replace(minute=0)
    t1 = closure.end + timedelta(hours=3)
    points = []
    t = t0
    while t <= t1:
        row: dict[str, Any] = {"t": t.isoformat(), "phase": cal.phase(t), "tokens": {}}
        for sym in symbols:
            v = history_view(book, sym, t)
            d = evaluate(OrderIntent(sym, "buy", order_usd), v, account, policy)
            row["tokens"][sym] = {
                "per_share": v.per_share, "quote_age_h": None if v.quote_age_s is None else round(v.quote_age_s / 3600, 2),
                "drift": v.drift, "dispersion": v.dispersion, "verdict": d.verdict, "allowed_usd": d.allowed_usd,
                "binding": d.binding,
            }
        row["reference"] = book.reference(ticker, t)[0]
        row["reference_age_h"] = v.reference_age_h
        points.append(row)
        t += timedelta(minutes=step_min)

    key_times = {
        "fri_open": closure.start - timedelta(hours=2),
        "fri_pre_close": closure.start - timedelta(minutes=30),
        "sat_noon": closure.start + timedelta(hours=20),
        "sun_evening": closure.end - timedelta(hours=13, minutes=30),
        "mon_pre_open": closure.end - timedelta(minutes=90),
        "mon_after_open": closure.end + timedelta(minutes=90),
    }
    snapshots = {}
    for name, kt in key_times.items():
        kt = kt.replace(second=0, microsecond=0)
        snapshots[name] = {sym: evaluate(OrderIntent(sym, "buy", order_usd), history_view(book, sym, kt),
                                         account, policy).to_dict() for sym in symbols}

    a = book.anchor(ticker)
    obs = [o for o in observe_weekends(a.before(closure.end + timedelta(hours=2))) if o.close_at == closure.start]
    model = book.gap_model(ticker, closure.start + timedelta(minutes=1))
    outcome = None
    if obs:
        o = obs[0]
        m = a.token.multiplier or 1.0          # report per share, like every other price on the page
        outcome = {"friday_close": o.friday_close / m,
                   "weekend_last": None if o.weekend_last is None else o.weekend_last / m,
                   "monday_ref": o.monday_ref / m,
                   "gap": o.gap, "drift": o.drift, "residual": o.residual,
                   "q99_before": model.q99 if model else None,
                   "exceeded_q99": (abs(o.gap) > model.q99) if model and model.q99 else None}
    return {
        "ticker": ticker, "symbols": symbols,
        "providers": {s: group[s].token.provider for s in symbols},
        "addresses": {s: group[s].token.address for s in symbols},
        "closure": {"start": closure.start.isoformat(), "end": closure.end.isoformat(), "kind": closure.kind,
                    "hours": round(closure.hours, 1)},
        "order_usd": order_usd, "account": account.to_dict(), "policy": policy.to_dict(),
        "gap_model": None if model is None else {
            "q99": model.q99, "q95": model.q95, "sigma_daily": model.sigma_daily, "sigma_gap": model.sigma_gap,
            "weekends_observed": model.n, "notes": model.notes},
        "points": points, "snapshots": snapshots, "outcome": outcome,
    }


def calibration(book: HistoryBook, ticker: str) -> dict[str, Any]:
    """Out-of-sample check: for every weekend, fit the model on data before Friday's close, then compare
    the realised Monday gap with the q99 the guard would have used. Also: how much of Monday's move had
    weekend on-chain trading already shown (only weekends that had weekend candles)."""
    a = book.anchor(ticker)
    rows = []
    for o in observe_weekends(a.candles):
        model = book.gap_model(ticker, o.close_at + timedelta(minutes=1))
        if model is None or model.q99 is None or model.returns_used < 20:
            continue
        rows.append({"friday": o.close_at.astimezone(cal.ET).date().isoformat(), "kind": o.kind,
                     "gap": o.gap, "drift": o.drift, "residual": o.residual, "q99": model.q99,
                     "exceeded": abs(o.gap) > model.q99})
    gaps = [abs(r["gap"]) for r in rows]
    with_weekend = [r for r in rows if r["drift"] is not None]
    priced_in = None
    corr = None
    if len(with_weekend) >= 5:
        mg = statistics.fmean(abs(r["gap"]) for r in with_weekend)
        mr = statistics.fmean(abs(r["residual"]) for r in with_weekend)
        priced_in = None if mg == 0 else 1 - mr / mg
        xs = [r["drift"] for r in with_weekend]
        ys = [r["gap"] for r in with_weekend]
        if statistics.pstdev(xs) > 0 and statistics.pstdev(ys) > 0:
            corr = statistics.correlation(xs, ys)
    return {
        "ticker": ticker, "anchor": a.token.symbol, "weekends": len(rows),
        "exceed_q99": sum(r["exceeded"] for r in rows),
        "max_abs_gap": max(gaps) if gaps else None,
        "mean_abs_gap": statistics.fmean(gaps) if gaps else None,
        "median_q99": statistics.median([r["q99"] for r in rows]) if rows else None,
        "weekends_with_weekend_trading": len(with_weekend),
        "priced_in_share": priced_in, "drift_gap_corr": corr,
        "rows": rows,
    }


def stress(book: HistoryBook, tickers: list[str], *, order_usd: float = 2000.0,
           account: AccountState | None = None, policy: GuardPolicy = REPLAY_POLICY,
           entry_offset_h: float = 20.0) -> dict[str, Any]:
    """Buy `order_usd` of the Ondo token on Saturday (closure start + 20 h ≈ Sat noon ET) every weekend
    that had weekend trading; mark to market at Monday 10:00 ET. Compare the guarded size with the raw one.
    """
    account = account or demo_account()
    trades = []
    for ticker in tickers:
        a = book.anchor(ticker)
        if a is None:
            continue
        for c in weekends(book, ticker):
            entry_t = c.start + timedelta(hours=entry_offset_h)
            v = history_view(book, a.token.symbol, entry_t)
            if v.per_share is None or v.quote_age_s is None or v.quote_age_s > REPLAY_QUOTE_AGE_S:
                continue                     # no weekend trading that Saturday: nothing to buy
            exit_got = a.last_at(c.end + timedelta(minutes=30))
            if exit_got is None:
                continue
            ret = exit_got[0] / v.price - 1
            d = evaluate(OrderIntent(a.token.symbol, "buy", order_usd), v, account, policy)
            trades.append({"ticker": ticker, "friday": c.start.astimezone(cal.ET).date().isoformat(),
                           "ret": ret, "raw_pnl": order_usd * ret, "guarded_usd": d.allowed_usd,
                           "guarded_pnl": d.allowed_usd * ret, "verdict": d.verdict})
    if not trades:
        return {"trades": [], "n": 0}
    raw = sorted(t["raw_pnl"] for t in trades)
    grd = sorted(t["guarded_pnl"] for t in trades)
    q = lambda xs, p: xs[max(0, min(len(xs) - 1, int(math.floor(p * (len(xs) - 1)))))]
    return {
        "n": len(trades), "order_usd": order_usd,
        "raw": {"worst": raw[0], "p05": q(raw, 0.05), "mean": statistics.fmean(raw), "best": raw[-1],
                "breach_daily_limit": sum(1 for x in raw if -x > account.daily_loss_limit)},
        "guarded": {"worst": grd[0], "p05": q(grd, 0.05), "mean": statistics.fmean(grd), "best": grd[-1],
                    "breach_daily_limit": sum(1 for x in grd if -x > account.daily_loss_limit),
                    "avg_size": statistics.fmean(t["guarded_usd"] for t in trades)},
        "trades": trades,
    }
