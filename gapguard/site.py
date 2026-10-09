"""Build the static dashboard's data files (docs/data/*.json) from recorded data — fully offline.

The page itself (docs/index.html, docs/app.js, docs/style.css) is static and reads only these files,
so it can be served by GitHub Pages with no backend and no API key in the browser.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .market import calendar as cal
from .risk.account import demo_account
from .risk.engine import OrderIntent, evaluate
from .risk.policy import GuardPolicy
from .scenarios import REPLAY_POLICY, calibration, find_weekend, stress, weekend_timeline
from .store import LIVE_DIR, iter_live
from .views import HistoryBook, history_view, recorded_view

UTC = timezone.utc

# Real weekends chosen for the story (all from recorded BSC data):
FEATURED = [
    ("TSLA", "2026-10-02", "Last complete weekend before the deadline. Ondo and bStocks track each other; "
                           "the xStocks pool on BSC trades a few times a week, so the guard refuses to mark it."),
    ("MSTR", "2026-09-18", "Largest MSTR Monday gap in the record (+8.9 %). The token climbs into Monday's "
                           "pre-open; once the drift passes 1 σ of a weekend move the cap is halved."),
    ("SPY", "2026-09-04", "Labor Day long weekend: 89.5 h without a stock print. Deeper position cut and a wider "
                          "gap than an ordinary weekend."),
    ("NVDA", "2026-09-11", "On-chain sell-off over the weekend (−1.7 % by Sunday night); Monday opened −3.4 %."),
    ("QQQ", "2026-06-12", "Early days of Ondo weekend trading: hours with no on-chain quote are blocked, "
                          "and a +2 % drift halves the cap."),
]
ORDER_USD = 2000.0


def _dump(path: Path, obj: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str), encoding="utf-8")
    return path


SLIM_METRICS = ("phase", "closure_kind", "closure_end", "per_share", "reference_price", "reference_age_h",
                "quote_age_s", "drift", "dispersion", "premium", "gap_q99", "gap_sigma", "gap_weekends_observed",
                "gap_q99_effective", "risk_move", "worst_per_usd", "open_planned_risk", "max_new_planned_risk",
                "stock_price", "price", "multiplier")


def _slim(d: dict) -> dict:
    d = dict(d)
    d["metrics"] = {k: d["metrics"].get(k) for k in SLIM_METRICS if k in d["metrics"]}
    for c in d["checks"]:
        c.pop("data", None)
    return d


def live_board(book: HistoryBook, records: list[dict], policy: GuardPolicy) -> dict[str, Any] | None:
    if not records:
        return None
    acc = demo_account()
    cycles = []
    for rec in records:
        row = {"ts": datetime.fromtimestamp(rec["ts"] / 1000, tz=UTC).isoformat(),
               "market_status": rec.get("market_status"), "tokens": {}}
        for sym in sorted(rec.get("tokens", {})):
            v = recorded_view(rec, sym, book)
            d = evaluate(OrderIntent(sym, "buy", ORDER_USD), v, acc, policy)
            row["tokens"][sym] = {"ticker": v.token.ticker, "type": v.token.type_id, "per_share": v.per_share,
                                  "stock_price": v.stock_price, "dispersion": v.dispersion, "drift": v.drift,
                                  "quote_age_s": v.quote_age_s, "verdict": d.verdict, "allowed_usd": d.allowed_usd,
                                  "binding": d.binding, "phase": d.phase}
        cycles.append(row)
    last = records[-1]
    detail = {}
    for sym in sorted(last.get("tokens", {})):
        v = recorded_view(last, sym, book)
        detail[sym] = _slim(evaluate(OrderIntent(sym, "buy", ORDER_USD), v, acc, policy).to_dict())
    return {"cycles": cycles, "latest": cycles[-1], "latest_detail": detail,
            "first_ts": cycles[0]["ts"], "last_ts": cycles[-1]["ts"], "n_cycles": len(cycles)}


def build(out: Path, *, weekends: int = 4) -> list[Path]:
    out = Path(out)
    book = HistoryBook.load()
    written = []
    scen_index = []
    for ticker, friday, blurb in FEATURED:
        try:
            c = find_weekend(book, ticker, friday)
        except ValueError:
            continue
        tl = weekend_timeline(book, ticker, c, order_usd=ORDER_USD)
        tl["blurb"] = blurb
        # full decisions every 2 h (the page snaps a click to the nearest one); hourly rows stay compact
        acc = demo_account()
        tl["decisions"] = {}
        reopen = datetime.fromisoformat(tl["closure"]["end"])
        for i, p in enumerate(tl["points"]):
            t = datetime.fromisoformat(p["t"])
            near_edge = p["phase"] in ("pre_close", "pre_open") or abs((t - reopen).total_seconds()) <= 3 * 3600
            if i % 2 and not near_edge:
                continue
            tl["decisions"][p["t"]] = {sym: _slim(evaluate(OrderIntent(sym, "buy", ORDER_USD),
                                                           history_view(book, sym, t), acc, REPLAY_POLICY).to_dict())
                                       for sym in tl["symbols"]}
        tl.pop("snapshots", None)
        tl.pop("policy", None)
        name = f"scenario_{ticker}_{friday}.json"
        written.append(_dump(out / name, tl))
        scen_index.append({"file": name, "ticker": ticker, "friday": friday, "blurb": blurb,
                           "kind": c.kind, "hours": round(c.hours, 1),
                           "gap": tl["outcome"]["gap"] if tl["outcome"] else None})

    cals = [calibration(book, t) for t in sorted(book.by_ticker)]
    written.append(_dump(out / "calibration.json", cals))
    st = stress(book, sorted(book.by_ticker), order_usd=5000.0)
    written.append(_dump(out / "stress.json", st))

    records = list(iter_live(LIVE_DIR))
    board = live_board(book, records, GuardPolicy())
    if board:
        written.append(_dump(out / "live.json", board))

    coverage = defaultdict(dict)
    for sym, s in book.series.items():
        if s.candles:
            coverage[s.token.ticker][sym] = {
                "provider": s.token.provider, "address": s.token.address, "candles": len(s.candles),
                "first": datetime.fromtimestamp(s.candles[0].open_ms / 1000, tz=UTC).isoformat(),
                "last": datetime.fromtimestamp(s.candles[-1].open_ms / 1000, tz=UTC).isoformat()}
    meta = {
        "version": __version__,
        "built_at": datetime.now(UTC).isoformat(),
        "order_usd": ORDER_USD,
        "account": demo_account().to_dict(),
        "policy": GuardPolicy().to_dict(),
        "replay_quote_age_s": REPLAY_POLICY.max_quote_age_s,
        "scenarios": scen_index,
        "coverage": coverage,
        "calibration_total": {"ticker_weekends": sum(c["weekends"] for c in cals),
                              "exceed_q99": sum(c["exceed_q99"] for c in cals)},
        "live": None if not board else {"first_ts": board["first_ts"], "last_ts": board["last_ts"],
                                        "n_cycles": board["n_cycles"]},
        "sources": {
            "kline": "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/dex/market/token/kline/ai",
            "dynamic": "https://www.binance.com/bapi/defi/v2/public/wallet-direct/buw/wallet/market/token/rwa/dynamic/ai",
            "token_list": "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/rwa/stock/detail/list/ai",
            "market_status": "https://www.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/market/token/rwa/market/status/ai",
            "rpc": "https://bsc-dataseed.bnbchain.org",
        },
    }
    written.append(_dump(out / "meta.json", meta))
    return written
