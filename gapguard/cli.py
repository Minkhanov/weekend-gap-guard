"""gapguard — command line. Run `python -m gapguard --help`.

Live commands use only public, key-less endpoints (Binance Wallet Skills RWA API, BSC JSON-RPC).
Replay commands (--at, scenario, gaps, stress, site) run fully offline from data/.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

UTC = timezone.utc


def _client():
    from .sources.binance_rwa import RwaClient
    return RwaClient()


def _parse_at(s: str) -> datetime:
    s = s.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).astimezone(UTC)


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, default=str)


def _account(args):
    from .risk.account import AccountState, demo_account
    return AccountState.load(args.account) if getattr(args, "account", None) else demo_account()


def _policy(args, replay: bool):
    from dataclasses import replace

    from .risk.policy import GuardPolicy
    from .views import REPLAY_QUOTE_AGE_S
    pol = GuardPolicy.load(getattr(args, "policy", None))
    if replay and pol.max_quote_age_s < REPLAY_QUOTE_AGE_S:
        pol = replace(pol, max_quote_age_s=REPLAY_QUOTE_AGE_S)
    return pol


# --------------------------------------------------------------------------------------------- check
def _view_for(args):
    """Return (view, source_label). Live by default; --at replays history; --recorded uses data/live."""
    from .views import HistoryBook, history_view, live_view, recorded_view
    if args.at:
        book = HistoryBook.load()
        if args.symbol not in book.series:
            raise SystemExit(f"no recorded history for {args.symbol}; run `gapguard history` first")
        return history_view(book, args.symbol, _parse_at(args.at)), True
    if args.recorded:
        from .store import iter_live
        target = None if args.recorded == "latest" else _parse_at(args.recorded)
        best = None
        for rec in iter_live():
            if args.symbol not in rec.get("tokens", {}):
                continue
            ts = datetime.fromtimestamp(rec["ts"] / 1000, tz=UTC)
            if target is None or ts <= target:
                best = rec
        if best is None:
            raise SystemExit("no recorded cycle at/before that time")
        book = HistoryBook.load()
        return recorded_view(best, args.symbol, book), False
    view, _ = live_view(_client(), args.symbol)
    return view, False


def cmd_check(args) -> int:
    from .risk.engine import OrderIntent, evaluate
    view, replay = _view_for(args)
    order = OrderIntent(args.symbol, args.side, args.usd, args.stop)
    d = evaluate(order, view, _account(args), _policy(args, replay))
    out = d.to_dict()
    if args.wallet_plan:
        from .integrations.agentic_wallet import plan
        out["agentic_wallet"] = plan(d, view.token.address, view.price, pay=args.pay).to_dict()
    if args.json:
        print(_dump(out))
    else:
        print(d.render())
        print(f"  source: {view.source}")
        if args.wallet_plan:
            w = out["agentic_wallet"]
            print(f"\n  Agentic Wallet hand-off (not executed): {w['reason']}")
            if w["quote_cmd"]:
                print(f"    {w['quote_cmd']}\n    {w['swap_cmd']}")
    return {"ALLOW": 0, "REDUCE": 0, "BLOCK": 3}[d.verdict] if args.exit_code else 0


# --------------------------------------------------------------------------------------------- status
def cmd_status(args) -> int:
    from .market import calendar as cal
    now = _parse_at(args.at) if args.at else datetime.now(UTC)
    c = cal.current_or_next_closure(now)
    info = {"now_utc": now.isoformat(), "phase": cal.phase(now), "regular_open": cal.is_regular_open(now),
            "last_close": cal.last_close(now).isoformat(), "next_open": cal.next_open(now).isoformat(),
            "closure": {"kind": c.kind, "start": c.start.isoformat(), "end": c.end.isoformat(),
                        "hours": round(c.hours, 1), "holidays": c.holidays}}
    if not args.at and not args.offline:
        ms = _client().market_status()
        f = lambda v: datetime.fromtimestamp(v / 1000, tz=UTC).isoformat() if v else None
        info["ondo_market_status"] = {"open": ms.open_state, "session": ms.market_status, "reason": ms.reason_code,
                                      "next_open": f(ms.next_open_ms), "next_close": f(ms.next_close_ms),
                                      "offhours_open": f(ms.offhours_open_ms), "offhours_close": f(ms.offhours_close_ms),
                                      "offhours_state": ms.offhours_open_state}
    print(_dump(info))
    return 0


# --------------------------------------------------------------------------------------------- research
def cmd_gaps(args) -> int:
    from .scenarios import calibration
    from .views import HistoryBook
    book = HistoryBook.load()
    tickers = args.tickers or sorted(book.by_ticker)
    res = [calibration(book, t) for t in tickers]
    if args.json:
        print(_dump(res if args.rows else [{k: v for k, v in r.items() if k != "rows"} for r in res]))
        return 0
    print(f"{'ticker':6s} {'weekends':>8s} {'>q99':>5s} {'max|gap|':>9s} {'mean|gap|':>9s} {'median q99':>10s} "
          f"{'wkd-trading':>11s} {'priced-in':>9s} {'corr':>6s}")
    for r in res:
        pi = "n/a" if r["priced_in_share"] is None else f"{r['priced_in_share']:+.0%}"
        co = "n/a" if r["drift_gap_corr"] is None else f"{r['drift_gap_corr']:+.2f}"
        print(f"{r['ticker']:6s} {r['weekends']:8d} {r['exceed_q99']:5d} {r['max_abs_gap']:9.2%} {r['mean_abs_gap']:9.2%} "
              f"{r['median_q99']:10.2%} {r['weekends_with_weekend_trading']:11d} {pi:>9s} {co:>6s}")
    print("\n>q99: Monday gaps larger than the q99 the guard would have used that Friday (out-of-sample).")
    print("priced-in: 1 - mean|Sun 20:00 ET -> Mon 10:00 ET| / mean|Fri close -> Mon 10:00 ET|, weekends with on-chain weekend trading only.")
    return 0


def cmd_scenario(args) -> int:
    from .scenarios import find_weekend, weekend_timeline
    from .views import HistoryBook
    book = HistoryBook.load()
    c = find_weekend(book, args.ticker, args.friday)
    tl = weekend_timeline(book, args.ticker, c, order_usd=args.usd, account=_account(args), step_min=args.step)
    if args.json:
        Path(args.json).write_text(_dump(tl), encoding="utf-8")
        print(f"wrote {args.json}")
        return 0
    syms = tl["symbols"]
    print(f"{args.ticker} weekend {c.start:%a %Y-%m-%d %H:%M} → {c.end:%a %Y-%m-%d %H:%M} UTC ({c.kind}, {c.hours:.0f} h); "
          f"order: buy {args.usd:,.0f} USD; q99 gap {tl['gap_model']['q99']:.2%}")
    print(f"{'UTC':16s} {'phase':9s} {'ref age':>7s} " + " ".join(f"{s:>22s}" for s in syms))
    for p in tl["points"]:
        cells = []
        for s in syms:
            x = p["tokens"][s]
            ps = "   --  " if x["per_share"] is None else f"{x['per_share']:8.2f}"
            cells.append(f"{ps} {x['verdict']:6s} {x['allowed_usd']:6,.0f}")
        age = "" if p["reference_age_h"] is None else f"{p['reference_age_h']:6.1f}h"
        print(f"{p['t'][:16]:16s} {p['phase']:9s} {age:>7s} " + " ".join(f"{c:>22s}" for c in cells))
    o = tl["outcome"]
    if o:
        print(f"\nWhat Monday did: Fri close {o['friday_close']:.2f} → Mon 10:00 ET {o['monday_ref']:.2f} = gap {o['gap']:+.2%}"
              f" (q99 before the weekend {o['q99_before']:.2%}; weekend drift {o['drift']:+.2%})" if o["drift"] is not None
              else f"\nWhat Monday did: gap {o['gap']:+.2%} (q99 before {o['q99_before']:.2%})")
    return 0


def cmd_stress(args) -> int:
    from .scenarios import stress
    from .views import HistoryBook
    book = HistoryBook.load()
    tickers = args.tickers or sorted(book.by_ticker)
    s = stress(book, tickers, order_usd=args.usd, account=_account(args))
    if args.json:
        print(_dump(s))
        return 0
    print(f"Saturday-noon buys of {args.usd:,.0f} USD, marked at Monday 10:00 ET — {s['n']} real weekends "
          f"({', '.join(tickers)})")
    for k in ("raw", "guarded"):
        r = s[k]
        print(f"  {k:8s} worst {r['worst']:+9,.2f}  5%-tail {r['p05']:+9,.2f}  mean {r['mean']:+8,.2f}  best {r['best']:+9,.2f}  "
              f"daily-limit breaches {r['breach_daily_limit']}")
    print(f"  guarded average size {s['guarded']['avg_size']:,.0f} USD")
    return 0


# --------------------------------------------------------------------------------------------- data
def cmd_history(args) -> int:
    from .collect import DEFAULT_TICKERS, fetch_history, universe
    client = _client()
    tokens = universe(client, args.tickers or DEFAULT_TICKERS)
    counts = fetch_history(client, tokens, days=args.days)
    for sym, n in counts.items():
        print(f"{sym:10s} {n:6d} candles")
    print(f"saved {len(counts)} histories, {client.http.calls} requests")
    return 0 if counts else 1


def cmd_record(args) -> int:
    from .collect import DEFAULT_TICKERS, record_loop, record_once, universe
    client = _client()
    tokens = universe(client, args.tickers or DEFAULT_TICKERS)
    if args.once:
        rec = record_once(client, tokens)
        print(f"recorded {len(rec['tokens'])} tokens, {len(rec['errors'])} errors")
        return 0
    until = int(_parse_at(args.until).timestamp() * 1000) if args.until else None
    cycles = record_loop(client, tokens, every_s=args.every, until_ms=until)
    print(f"recorder stopped after {cycles} cycles")
    return 0


def cmd_portfolio(args) -> int:
    from .portfolio import scan
    from .sources.bsc_rpc import DEFAULT_RPC, BscRpc
    from .views import HistoryBook
    book = HistoryBook.load()
    res = scan(_client(), BscRpc(args.rpc or DEFAULT_RPC), args.address, book)
    if args.json:
        print(_dump(res))
        return 0
    print(f"{args.address}: {len(res['holdings'])} stock tokens held (scanned {res['tokens_scanned']} on BSC via {res['rpc']})")
    for r in res["holdings"]:
        usd = "n/a" if r["usd"] is None else f"{r['usd']:,.2f}"
        print(f"  {r['symbol']:10s} {r['provider']:8s} {r['amount']:>14,.6f}  ≈ {usd:>12s} USD  q99 gap {r['gap_q99']:.1%} ({r['gap_q99_source']})")
    print(f"  total ≈ {res['total_usd']:,.2f} USD; weekend loss at q99 ≈ {res['weekend_loss_q99_usd']:,.2f} USD")
    return 0


def cmd_site(args) -> int:
    from .site import build
    paths = build(Path(args.out), weekends=args.weekends)
    for p in paths:
        print(f"wrote {p}")
    return 0


# --------------------------------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gapguard",
                                description="Weekend Gap Guard — pre-trade risk gate for tokenized stocks on BNB Chain")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="ALLOW / REDUCE / BLOCK one order, with every check explained")
    c.add_argument("symbol", help="token symbol, e.g. TSLAon (Ondo), TSLAx (xStocks), TSLAB (bStocks)")
    c.add_argument("--usd", type=float, required=True, help="order notional in USD")
    c.add_argument("--side", choices=["buy", "sell"], default="buy")
    c.add_argument("--stop", type=float, help="planned stop distance as a fraction, e.g. 0.03")
    c.add_argument("--account", help="account JSON (default: demo 10k prop-style account)")
    c.add_argument("--policy", help="policy JSON with overrides")
    src = c.add_mutually_exclusive_group()
    src.add_argument("--at", help="replay from data/history at this UTC time, e.g. 2026-10-03T16:00Z")
    src.add_argument("--recorded", help="use a recorded live cycle: 'latest' or an ISO time")
    c.add_argument("--wallet-plan", action="store_true", help="also print the Agentic Wallet (baw) commands — never executed")
    c.add_argument("--pay", default="USDT", help="stablecoin for the wallet plan (USDT, USDC, USD1)")
    c.add_argument("--json", action="store_true")
    c.add_argument("--exit-code", action="store_true", help="exit 3 on BLOCK (for scripts and agents)")
    c.set_defaults(func=cmd_check)

    s = sub.add_parser("status", help="session clock: phase, current/next closure, live Ondo market status")
    s.add_argument("--at")
    s.add_argument("--offline", action="store_true")
    s.set_defaults(func=cmd_status)

    g = sub.add_parser("gaps", help="out-of-sample calibration of the weekend-gap model on recorded history")
    g.add_argument("tickers", nargs="*")
    g.add_argument("--json", action="store_true")
    g.add_argument("--rows", action="store_true", help="with --json: include every weekend")
    g.set_defaults(func=cmd_gaps)

    sc = sub.add_parser("scenario", help="replay one real weekend hour by hour for all issuers of a ticker")
    sc.add_argument("ticker")
    sc.add_argument("--friday", required=True, help="ET date of the Friday close, e.g. 2026-10-02")
    sc.add_argument("--usd", type=float, default=2000.0)
    sc.add_argument("--step", type=int, default=60, help="minutes between rows")
    sc.add_argument("--account")
    sc.add_argument("--json", help="write the full timeline to this file")
    sc.set_defaults(func=cmd_scenario)

    st = sub.add_parser("stress", help="Saturday buys vs Monday open across all recorded weekends, guarded vs raw")
    st.add_argument("tickers", nargs="*")
    st.add_argument("--usd", type=float, default=5000.0)
    st.add_argument("--account")
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_stress)

    pf = sub.add_parser("portfolio", help="watch-only: stock tokens held by a BSC address + weekend gap exposure")
    pf.add_argument("address")
    pf.add_argument("--rpc")
    pf.add_argument("--json", action="store_true")
    pf.set_defaults(func=cmd_portfolio)

    h = sub.add_parser("history", help="download hourly candles (public K-Line endpoint) into data/history")
    h.add_argument("--tickers", nargs="*")
    h.add_argument("--days", type=int, default=400)
    h.set_defaults(func=cmd_history)

    r = sub.add_parser("record", help="poll market status + RWA Dynamic V2 into data/live/*.jsonl")
    r.add_argument("--tickers", nargs="*")
    r.add_argument("--every", type=int, default=600, help="seconds between cycles (default 600)")
    r.add_argument("--until", help="ISO time (UTC) to stop, e.g. 2026-10-11T12:00Z")
    r.add_argument("--once", action="store_true")
    r.set_defaults(func=cmd_record)

    si = sub.add_parser("site", help="build the static dashboard data (docs/data/*.json)")
    si.add_argument("--out", default="docs/data")
    si.add_argument("--weekends", type=int, default=4, help="how many recent weekends per ticker to export")
    si.set_defaults(func=cmd_site)
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")      # Windows consoles
    except (AttributeError, ValueError):
        pass
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from .sources.http import SourceError
    try:
        return int(args.func(args) or 0)
    except SourceError as exc:
        print(f"data source error: {exc}", file=sys.stderr)
        return 2
