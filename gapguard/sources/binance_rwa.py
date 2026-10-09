"""Binance Wallet Skills — tokenized-securities endpoints (public, no API key).

Endpoints as published in binance/binance-skills-hub, skill `binance-tokenized-securities-info`
(v1.1). Base: https://www.binance.com/bapi/defi. Response envelope:
``{"code": "000000", "data": ..., "success": true}``.

Record / replay: pass `record_dir` to save every JSON answer, `replay_dir` to serve answers from
such a directory with no network at all (used by the offline demo and by the tests).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from ..models import BSC_CHAIN_ID, AssetStatus, Candle, MarketStatus, TokenRef, TokenSnapshot
from .http import PoliteClient, SourceError

log = logging.getLogger("gapguard.rwa")

BASE = "https://www.binance.com/bapi/defi"
EP = {
    "token_list": "/v1/public/wallet-direct/buw/wallet/market/token/rwa/stock/detail/list/ai",
    "meta": "/v1/public/wallet-direct/buw/wallet/market/token/rwa/meta/ai",
    "market_status": "/v1/public/wallet-direct/buw/wallet/market/token/rwa/market/status/ai",
    "asset_status": "/v1/public/wallet-direct/buw/wallet/market/token/rwa/asset/market/status/ai",
    "dynamic": "/v2/public/wallet-direct/buw/wallet/market/token/rwa/dynamic/ai",
    "kline": "/v1/public/wallet-direct/buw/wallet/dex/market/token/kline/ai",
}
HEADERS = {"Accept-Encoding": "identity", "User-Agent": "binance-web3/1.1 (Skill)"}
KLINE_MAX = 300
INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000,
               "12h": 43_200_000, "1d": 86_400_000}


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _i(value: Any) -> int | None:
    v = _f(value)
    return int(v) if v is not None else None


def cache_key(name: str, params: dict[str, Any] | None) -> str:
    items = "&".join(f"{k}={params[k]}" for k in sorted(params or {}))
    slug = re.sub(r"[^A-Za-z0-9]+", "_", items)[:60].strip("_")
    digest = hashlib.sha1(items.encode()).hexdigest()[:8]
    return f"{name}__{slug}__{digest}.json" if items else f"{name}.json"


class RwaClient:
    def __init__(self, http: PoliteClient | None = None, *, record_dir: str | Path | None = None,
                 replay_dir: str | Path | None = None, base: str = BASE):
        self.http = http or PoliteClient(headers=HEADERS)
        self.base = base
        self.record_dir = Path(record_dir) if record_dir else None
        self.replay_dir = Path(replay_dir) if replay_dir else None
        self._tokens: list[TokenRef] | None = None

    # ---- transport -------------------------------------------------------------------------
    def _get(self, name: str, params: dict[str, Any] | None = None) -> Any:
        key = cache_key(name, params)
        if self.replay_dir is not None:
            path = self.replay_dir / key
            if not path.exists():
                raise SourceError(f"offline snapshot has no {key}")
            body = json.loads(path.read_text(encoding="utf-8"))
        else:
            body = self.http.get_json(self.base + EP[name], params=params)
            if self.record_dir is not None:
                self.record_dir.mkdir(parents=True, exist_ok=True)
                (self.record_dir / key).write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        if not isinstance(body, dict) or str(body.get("code")) != "000000" or body.get("success") is False:
            code = body.get("code") if isinstance(body, dict) else "?"
            msg = body.get("message") if isinstance(body, dict) else str(body)[:80]
            raise SourceError(f"{name}: API code {code}: {msg}")
        return body.get("data")

    def raw(self, name: str, params: dict[str, Any] | None = None) -> Any:
        """The unparsed `data` object of one endpoint (used by the recorder, which stores raw answers)."""
        return self._get(name, params)

    # ---- token universe --------------------------------------------------------------------
    def tokens(self, chain_id: str = BSC_CHAIN_ID) -> list[TokenRef]:
        if self._tokens is None:
            data = self._get("token_list") or []
            out = []
            for item in data:
                try:
                    out.append(TokenRef(
                        chain_id=str(item["chainId"]), address=str(item["contractAddress"]).lower(),
                        symbol=str(item["symbol"]), ticker=str(item.get("ticker") or ""),
                        type_id=int(item.get("type") or 0), multiplier=_f(item.get("multiplier")) or 1.0,
                        decimals=int(item.get("d") or 18)))
                except (KeyError, TypeError, ValueError):
                    log.debug("skipping malformed token row %r", item)
            self._tokens = out
        return [t for t in self._tokens if t.chain_id == chain_id]

    def resolve(self, symbol: str, chain_id: str = BSC_CHAIN_ID) -> TokenRef:
        """Resolve a token symbol (TSLAon, TSLAx, TSLAB) or a 0x address on the given chain."""
        want = symbol.strip()
        for t in self.tokens(chain_id):
            if t.symbol.lower() == want.lower() or t.address == want.lower():
                return t
        raise SourceError(f"unknown tokenized stock {symbol!r} on chain {chain_id}")

    def siblings(self, token: TokenRef) -> list[TokenRef]:
        """All issuers' tokens for the same underlying ticker on the same chain (Ondo, xStocks, bStocks)."""
        return [t for t in self.tokens(token.chain_id) if t.ticker == token.ticker and t.type_id in (1, 2, 3)]

    # ---- status ----------------------------------------------------------------------------
    def market_status(self) -> MarketStatus:
        d = self._get("market_status") or {}
        off = d.get("offhours") or {}
        return MarketStatus(
            open_state=bool(d.get("openState")), market_status=d.get("marketStatus"),
            reason_code=d.get("reasonCode"), reason_msg=d.get("reasonMsg"),
            next_open_ms=_i(d.get("nextOpenTime")), next_close_ms=_i(d.get("nextCloseTime")),
            offhours_open_ms=_i(off.get("nextOpenTime")), offhours_close_ms=_i(off.get("nextCloseTime")),
            offhours_open_state=off.get("openState") if off else None)

    def asset_status(self, token: TokenRef) -> AssetStatus:
        d = self._get("asset_status", {"chainId": token.chain_id, "contractAddress": token.address}) or {}
        return _asset_status(d)

    def snapshot(self, token: TokenRef) -> TokenSnapshot:
        d = self._get("dynamic", {"chainId": token.chain_id, "contractAddress": token.address}) or {}
        ti = d.get("tokenInfo") or {}
        si = d.get("stockInfo") or {}
        mult = _f(ti.get("sharesMultiplier")) or token.multiplier or 1.0
        return TokenSnapshot(
            token=token, price=_f(ti.get("price")), stock_price=_f(si.get("price")), multiplier=mult,
            status=_asset_status(d.get("statusInfo") or {}), holders=_i(ti.get("totalHolders")),
            week52_high=_f(si.get("priceHigh52w")), week52_low=_f(si.get("priceLow52w")),
            fetched_ms=int(time.time() * 1000))

    # ---- candles ---------------------------------------------------------------------------
    def klines(self, token: TokenRef, interval: str = "1h", limit: int = KLINE_MAX,
               end_ms: int | None = None) -> list[Candle]:
        params: dict[str, Any] = {"chainId": token.chain_id, "contractAddress": token.address,
                                  "interval": interval, "limit": min(limit, KLINE_MAX)}
        if end_ms is not None:
            params["endTime"] = end_ms
        d = self._get("kline", params) or {}
        out = []
        for row in d.get("klineInfos") or []:
            try:
                out.append(Candle(open_ms=int(row[0]), open=float(row[1]), high=float(row[2]), low=float(row[3]),
                                  close=float(row[4]), volume=_f(row[5]) or 0.0, close_ms=int(row[6])))
            except (IndexError, TypeError, ValueError):
                continue
        out.sort(key=lambda c: c.open_ms)
        return out

    def klines_back(self, token: TokenRef, interval: str, since_ms: int, *, end_ms: int | None = None,
                    max_pages: int = 12) -> list[Candle]:
        """Page backwards (endTime = first open − 1 ms, verified live) until `since_ms` is covered."""
        seen: dict[int, Candle] = {}
        cursor = end_ms
        for _ in range(max_pages):
            page = self.klines(token, interval, KLINE_MAX, end_ms=cursor)
            if not page:
                break
            for c in page:
                seen[c.open_ms] = c
            first = page[0].open_ms
            if first <= since_ms or len(page) < KLINE_MAX:
                break
            cursor = first - 1
        return [seen[k] for k in sorted(seen) if k >= since_ms]

    def close(self) -> None:
        self.http.close()


def _asset_status(d: dict[str, Any]) -> AssetStatus:
    return AssetStatus(
        open_state=d.get("openState"), market_status=d.get("marketStatus"), reason_code=d.get("reasonCode"),
        reason_msg=d.get("reasonMsg"), next_open_ms=_i(d.get("nextOpenTime")),
        next_close_ms=_i(d.get("nextCloseTime")))
