"""Data collection: hourly history for the gap model, and a slow live recorder for the weekend.

Both talk only to the public Binance Wallet Skills endpoints (no key) through the polite client
(≥ 0.5 s between requests). Nothing here signs, trades or needs an account.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from .models import TokenRef
from .sources.binance_rwa import BASE, EP, RwaClient
from .sources.http import SourceError
from .store import HISTORY_DIR, LIVE_DIR, TokenHistory, append_live, save_history

log = logging.getLogger("gapguard.collect")

# Ten liquid underlyings that exist under all three issuers on BSC (checked live on 2026-10-09).
DEFAULT_TICKERS = ["TSLA", "NVDA", "AAPL", "SPY", "QQQ", "COIN", "MSTR", "META", "AMZN", "CRCL"]
ISSUER_TYPES = (1, 2, 3)          # Ondo, xStocks, bStocks


def universe(client: RwaClient, tickers: list[str]) -> list[TokenRef]:
    want = {t.upper() for t in tickers}
    out = [t for t in client.tokens() if t.ticker.upper() in want and t.type_id in ISSUER_TYPES]
    return sorted(out, key=lambda t: (t.ticker, t.type_id))


def fetch_history(client: RwaClient, tokens: list[TokenRef], *, days: int = 400, interval: str = "1h",
                  base: Path = HISTORY_DIR, max_pages: int = 40) -> dict[str, int]:
    since = int((time.time() - days * 86400) * 1000)
    counts: dict[str, int] = {}
    for t in tokens:
        try:
            candles = client.klines_back(t, interval, since, max_pages=max_pages)
        except SourceError as exc:
            log.warning("history %s failed: %s", t.symbol, exc)
            continue
        h = TokenHistory(t, interval, candles, int(time.time() * 1000), BASE + EP["kline"])
        save_history(h, base)
        counts[t.symbol] = len(candles)
        log.info("history %s: %d candles", t.symbol, len(candles))
    return counts


KEEP = {
    "tokenInfo": ("price", "priceChange24h", "priceChangePct24h", "sharesMultiplier", "circulatingSupply",
                  "totalHolders", "volume24h"),
    "stockInfo": ("price", "volume"),
}


def trim_dynamic(d: dict | None) -> dict | None:
    """Keep the fields the guard reads; drop fundamentals (P/E, 52w...) that bloat a 48-hour recording.

    Kept verbatim (strings as the API sent them): tokenInfo price/multiplier/supply, stockInfo price,
    the whole statusInfo and limitInfo objects.
    """
    if not isinstance(d, dict):
        return d
    out: dict = {"symbol": d.get("symbol"), "ticker": d.get("ticker"), "type": d.get("type"),
                 "statusInfo": d.get("statusInfo"), "limitInfo": d.get("limitInfo")}
    for block, keys in KEEP.items():
        src = d.get(block) or {}
        out[block] = {k: src.get(k) for k in keys}
    return out


def record_once(client: RwaClient, tokens: list[TokenRef], *, base: Path = LIVE_DIR) -> dict:
    now = datetime.now(timezone.utc)
    rec: dict = {"ts": int(now.timestamp() * 1000), "market_status": None, "tokens": {}, "errors": {}}
    try:
        rec["market_status"] = client.raw("market_status")
    except SourceError as exc:
        rec["errors"]["market_status"] = str(exc)[:200]
    for t in tokens:
        try:
            rec["tokens"][t.symbol] = {
                "address": t.address, "ticker": t.ticker, "type": t.type_id,
                "dynamic": trim_dynamic(client.raw("dynamic", {"chainId": t.chain_id, "contractAddress": t.address})),
                "latency_ms": round(client.http.last_latency_ms or 0, 1),
            }
        except SourceError as exc:
            rec["errors"][t.symbol] = str(exc)[:200]
    append_live(rec, now.strftime("%Y-%m-%d"), base)
    return rec


def record_loop(client: RwaClient, tokens: list[TokenRef], *, every_s: int = 600, until_ms: int | None = None,
                base: Path = LIVE_DIR) -> int:
    cycles = 0
    while True:
        started = time.time()
        rec = record_once(client, tokens, base=base)
        cycles += 1
        log.info("live cycle %d: %d tokens, %d errors", cycles, len(rec["tokens"]), len(rec["errors"]))
        if until_ms is not None and time.time() * 1000 >= until_ms:
            return cycles
        time.sleep(max(5.0, every_s - (time.time() - started)))
