"""What the guard sees about one token at one instant: on-chain price vs. the frozen reference.

Vocabulary used everywhere (README, dashboard, decisions):

  reference price  the underlying's last regular-session close, expressed per share. Over a weekend this
                   is Friday 16:00 ET — the last price the stock market itself produced.
  reference age    hours since that close. 0 while the regular session is open.
  drift            on-chain per-share price ÷ reference − 1: how far weekend trading moved the token
                   away from the last real print.
  dispersion       |this token's per-share price ÷ median of all issuers (Ondo / xStocks / bStocks) − 1|:
                   how far this deployment sits from the cross-issuer consensus. A stale or broken pool
                   shows up here; a healthy token next to a broken sibling does not get punished.
  spread           max ÷ min − 1 across issuers (reported, not used for decisions).
  premium          per-share on-chain price ÷ live underlying print − 1 (only when a print exists).
  quote age        how old the on-chain price observation is (live: fetch time; replay: candle close).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..market import calendar as cal
from ..models import AssetStatus, TokenRef
from .gap import GapModel


@dataclass
class MarketView:
    at: datetime
    token: TokenRef
    price: float | None                       # on-chain price per token, USD
    multiplier: float
    quote_at: datetime | None
    reference_price: float | None             # per share, last regular close
    reference_at: datetime | None
    status: AssetStatus | None = None
    stock_price: float | None = None          # live underlying print, None when the market is shut
    siblings: dict[str, float] = field(default_factory=dict)   # symbol -> per-share price, same ticker
    issuer_limit_usd: float | None = None     # Ondo limitInfo.maxActiveNotionalValue
    gap: GapModel | None = None
    source: str = "live"
    notes: list[str] = field(default_factory=list)

    @property
    def per_share(self) -> float | None:
        if self.price is None or not self.multiplier:
            return None
        return self.price / self.multiplier

    @property
    def phase(self) -> str:
        return cal.phase(self.at)

    @property
    def closure(self) -> cal.Closure:
        return cal.current_or_next_closure(self.at)

    @property
    def regular_open(self) -> bool:
        return cal.is_regular_open(self.at)

    @property
    def reference_age_h(self) -> float | None:
        if self.regular_open:
            return 0.0
        if self.reference_at is None:
            return None
        return max(0.0, (self.at - self.reference_at).total_seconds() / 3600)

    @property
    def quote_age_s(self) -> float | None:
        if self.quote_at is None:
            return None
        return max(0.0, (self.at - self.quote_at).total_seconds())

    @property
    def drift(self) -> float | None:
        ps = self.per_share
        if ps is None or not self.reference_price:
            return None
        return ps / self.reference_price - 1

    @property
    def dispersion(self) -> float | None:
        me = self.per_share
        prices = sorted(p for p in [me, *self.siblings.values()] if p and p > 0)
        if me is None or len(prices) < 2:
            return None
        n = len(prices)
        median = prices[n // 2] if n % 2 else (prices[n // 2 - 1] + prices[n // 2]) / 2
        return abs(me / median - 1)

    @property
    def spread(self) -> float | None:
        prices = [p for p in [self.per_share, *self.siblings.values()] if p and p > 0]
        if len(prices) < 2:
            return None
        return max(prices) / min(prices) - 1

    @property
    def premium(self) -> float | None:
        if self.per_share is None or not self.stock_price:
            return None
        return self.per_share / self.stock_price - 1

    def metrics(self) -> dict:
        g = self.gap
        return {
            "source": self.source,
            "phase": self.phase,
            "closure_kind": self.closure.kind,
            "closure_start": self.closure.start.isoformat(),
            "closure_end": self.closure.end.isoformat(),
            "price": self.price,
            "per_share": self.per_share,
            "multiplier": self.multiplier,
            "stock_price": self.stock_price,
            "reference_price": self.reference_price,
            "reference_at": self.reference_at.isoformat() if self.reference_at else None,
            "reference_age_h": None if self.reference_age_h is None else round(self.reference_age_h, 2),
            "quote_age_s": None if self.quote_age_s is None else round(self.quote_age_s, 1),
            "drift": self.drift,
            "dispersion": self.dispersion,
            "spread": self.spread,
            "premium": self.premium,
            "siblings": self.siblings,
            "gap_q99": g.q99 if g else None,
            "gap_sigma": g.sigma_gap if g else None,
            "gap_weekends_observed": g.n if g else 0,
        }
