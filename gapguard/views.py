"""Build a MarketView from one of three sources — always using only data available at that instant.

  live       public RWA endpoints right now (Dynamic V2 for the token and its sibling issuers, K-Line for
             the reference close and the gap model)
  recorded   one cycle of our weekend recorder (data/live/*.jsonl) + hourly history for the reference
  history    hourly candles only (data/history), for replaying past weekends hour by hour

No look-ahead: the gap model and the reference close are computed from candles that closed before `at`.
The reference close is taken from the Ondo token of the same ticker (Ondo's oracle tracks the stock
within ~0.1 % during regular hours per the Binance skill docs); it falls back to the token itself.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .market import calendar as cal
from .models import AssetStatus, Candle, TokenRef
from .risk.gap import HOUR_MS, GapModel, build_model
from .risk.reference import MarketView
from .store import TokenHistory, available_history, load_history

UTC = timezone.utc
REPLAY_QUOTE_AGE_S = 2 * 3600       # hourly candles: a candle that closed ≤ 2 h ago counts as a live quote


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


@dataclass
class Series:
    """Hourly candles of one token with O(log n) 'last close at or before t'."""
    token: TokenRef
    candles: list[Candle]
    _ends: list[int] = field(default_factory=list, repr=False)

    def __post_init__(self):
        self.candles = sorted(self.candles, key=lambda c: c.open_ms)
        self._ends = [c.open_ms + HOUR_MS for c in self.candles]

    def last_at(self, t: datetime) -> tuple[float, datetime] | None:
        i = bisect_right(self._ends, _ms(t))
        if i == 0:
            return None
        c = self.candles[i - 1]
        return c.close, _dt(c.open_ms + HOUR_MS)

    def before(self, t: datetime) -> list[Candle]:
        i = bisect_right(self._ends, _ms(t))
        return self.candles[:i]

    @classmethod
    def from_history(cls, h: TokenHistory) -> "Series":
        return cls(h.token, h.candles)


class HistoryBook:
    """All recorded histories, grouped by ticker; caches gap models per (ticker, reference close)."""

    def __init__(self, series: dict[str, Series]):
        self.series = series
        self.by_ticker: dict[str, dict[str, Series]] = {}
        for sym, s in series.items():
            self.by_ticker.setdefault(s.token.ticker, {})[sym] = s
        self._gap_cache: dict[tuple[str, int], GapModel] = {}

    @classmethod
    def load(cls, symbols: list[str] | None = None) -> "HistoryBook":
        syms = symbols or available_history()
        return cls({s: Series.from_history(load_history(s)) for s in syms})

    def anchor(self, ticker: str) -> Series | None:
        """The series used for the reference close and the gap model: Ondo first, else the longest."""
        group = self.by_ticker.get(ticker) or {}
        ondo = [s for s in group.values() if s.token.type_id == 1]
        if ondo:
            return ondo[0]
        return max(group.values(), key=lambda s: len(s.candles), default=None)

    def reference(self, ticker: str, at: datetime) -> tuple[float | None, datetime]:
        close_at = cal.last_close(at)
        a = self.anchor(ticker)
        if a is None:
            return None, close_at
        got = a.last_at(close_at)
        if got is None or (close_at - got[1]) > timedelta(hours=6):
            return None, close_at
        return got[0] / (a.token.multiplier or 1.0), close_at

    def gap_model(self, ticker: str, at: datetime) -> GapModel | None:
        a = self.anchor(ticker)
        if a is None:
            return None
        close_at = cal.last_close(at)
        key = (ticker, _ms(close_at))
        if key not in self._gap_cache:
            closure = cal.current_or_next_closure(at)
            self._gap_cache[key] = build_model(ticker, a.before(close_at), closure=closure)
        return self._gap_cache[key]


def history_view(book: HistoryBook, symbol: str, at: datetime) -> MarketView:
    s = book.series[symbol]
    t = s.token
    got = s.last_at(at)
    price, quote_at = (got if got else (None, None))
    ref, ref_at = book.reference(t.ticker, at)
    siblings: dict[str, float] = {}
    for sym, other in (book.by_ticker.get(t.ticker) or {}).items():
        if sym == symbol:
            continue
        o = other.last_at(at)
        if o and (at - o[1]).total_seconds() <= REPLAY_QUOTE_AGE_S:
            siblings[sym] = o[0] / (other.token.multiplier or 1.0)
    return MarketView(at=at, token=t, price=price, multiplier=t.multiplier or 1.0, quote_at=quote_at,
                      reference_price=ref, reference_at=ref_at, status=None, stock_price=None, siblings=siblings,
                      issuer_limit_usd=None, gap=book.gap_model(t.ticker, at), source="replay:history-1h")


# ---------------------------------------------------------------------------------------------
# Raw Dynamic V2 objects (live API or a recorded cycle) → MarketView
# ---------------------------------------------------------------------------------------------
def _f(v: Any) -> float | None:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


def no_trade_24h(token_info: dict) -> bool:
    ch = token_info.get("priceChange24h")
    return ch is not None and _f(ch) == 0.0 and _f(token_info.get("priceChangePct24h")) in (0.0, None)


def status_from(d: dict | None) -> AssetStatus | None:
    if not d:
        return None
    return AssetStatus(open_state=d.get("openState"), market_status=d.get("marketStatus"),
                       reason_code=d.get("reasonCode"), reason_msg=d.get("reasonMsg"),
                       next_open_ms=d.get("nextOpenTime"), next_close_ms=d.get("nextCloseTime"))


def dynamic_view(token: TokenRef, dyn: dict, at: datetime, quote_at: datetime, book: HistoryBook | None,
                 sibling_dyn: dict[str, dict], source: str) -> MarketView:
    ti = dyn.get("tokenInfo") or {}
    si = dyn.get("stockInfo") or {}
    li = dyn.get("limitInfo") or {}
    mult = _f(ti.get("sharesMultiplier")) or token.multiplier or 1.0
    notes = []
    if no_trade_24h(ti):
        # The endpoint returns the last traded price with no timestamp. A 24 h change of exactly 0 means
        # nothing traded for at least a day (seen live on thin xStocks deployments): the quote is ≥ 24 h old.
        quote_at = at - timedelta(hours=24)
        notes.append("priceChange24h is exactly 0 — no trade for ≥ 24 h, quote age set to 24 h (lower bound)")
    siblings = {}
    for sym, d in sibling_dyn.items():
        sti = d.get("tokenInfo") or {}
        p = _f(sti.get("price"))
        m = _f(sti.get("sharesMultiplier")) or 1.0
        if p and not no_trade_24h(sti):
            siblings[sym] = p / m
    stock_price = _f(si.get("price"))
    if stock_price is None:
        # bStocks / xStocks rows carry no stock print (seen live): borrow it from a sibling of the same ticker.
        for d in sibling_dyn.values():
            stock_price = _f((d.get("stockInfo") or {}).get("price"))
            if stock_price:
                notes.append("live stock print borrowed from a sibling issuer (this token's stockInfo.price is null)")
                break
    ref, ref_at = (book.reference(token.ticker, at) if book else (None, cal.last_close(at)))
    return MarketView(at=at, token=token, price=_f(ti.get("price")), multiplier=mult, quote_at=quote_at,
                      reference_price=ref, reference_at=ref_at, status=status_from(dyn.get("statusInfo")),
                      stock_price=stock_price, siblings=siblings,
                      issuer_limit_usd=_f(li.get("maxActiveNotionalValue")),
                      gap=book.gap_model(token.ticker, at) if book else None, source=source, notes=notes)


def recorded_view(record: dict, symbol: str, book: HistoryBook | None) -> MarketView:
    at = _dt(int(record["ts"]))
    entry = record["tokens"][symbol]
    token = TokenRef(chain_id="56", address=entry["address"], symbol=symbol, ticker=entry["ticker"],
                     type_id=int(entry["type"]))
    sib = {s: e["dynamic"] for s, e in record["tokens"].items()
           if s != symbol and e.get("ticker") == entry["ticker"] and e.get("dynamic")}
    return dynamic_view(token, entry["dynamic"], at, at, book, sib, "recorded-live")


def live_view(client, symbol: str, *, history_days: int = 70) -> tuple[MarketView, HistoryBook]:
    """Everything from the public endpoints right now. ~10 requests, a few seconds at the polite pace."""
    from .collect import ISSUER_TYPES
    token = client.resolve(symbol)
    group = [t for t in client.tokens(token.chain_id) if t.ticker == token.ticker and t.type_id in ISSUER_TYPES]
    now = datetime.now(UTC)
    since = _ms(now - timedelta(days=history_days))
    series = {}
    anchor = next((t for t in group if t.type_id == 1), token)
    for t in {anchor.symbol: anchor, token.symbol: token}.values():
        series[t.symbol] = Series(t, client.klines_back(t, "1h", since))
    book = HistoryBook(series)
    dyn = client.raw("dynamic", {"chainId": token.chain_id, "contractAddress": token.address})
    quote_at = datetime.now(UTC)
    sib = {}
    for t in group:
        if t.symbol != token.symbol:
            sib[t.symbol] = client.raw("dynamic", {"chainId": t.chain_id, "contractAddress": t.address})
    return dynamic_view(token, dyn, quote_at, quote_at, book, sib, "live"), book
