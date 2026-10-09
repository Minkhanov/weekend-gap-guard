"""Plain data objects shared by the sources, the views and the risk engine."""
from __future__ import annotations

from dataclasses import dataclass

BSC_CHAIN_ID = "56"

# `type` in the Binance tokenized-stock list. 1/2/3 are the three issuers this hackathon names;
# other values exist in the live list (4, 5, 9, 11) but are undocumented, so we keep them labelled.
PROVIDERS: dict[int, str] = {1: "Ondo", 2: "xStocks", 3: "bStocks"}


def provider_name(type_id: int | None) -> str:
    if type_id is None:
        return "unknown"
    return PROVIDERS.get(type_id, f"type-{type_id}")


@dataclass(frozen=True)
class TokenRef:
    """One tokenized stock deployment (a ticker can have one token per issuer per chain)."""

    chain_id: str
    address: str
    symbol: str
    ticker: str
    type_id: int
    multiplier: float = 1.0
    decimals: int = 18

    @property
    def provider(self) -> str:
        return provider_name(self.type_id)


@dataclass(frozen=True)
class Candle:
    open_ms: int
    open: float
    high: float
    low: float
    close: float
    close_ms: int
    volume: float = 0.0


@dataclass(frozen=True)
class MarketStatus:
    """Overall session state of the tokenized-stock market (Ondo schedule)."""

    open_state: bool
    market_status: str | None          # premarket / regular / postmarket / overnight / closed / pause
    reason_code: str | None
    reason_msg: str | None
    next_open_ms: int | None
    next_close_ms: int | None
    offhours_open_ms: int | None = None   # weekend "off-hours" window (undocumented field, seen live)
    offhours_close_ms: int | None = None
    offhours_open_state: bool | None = None


@dataclass(frozen=True)
class AssetStatus:
    open_state: bool | None
    market_status: str | None
    reason_code: str | None
    reason_msg: str | None
    next_open_ms: int | None = None
    next_close_ms: int | None = None

    @property
    def corporate_action(self) -> str | None:
        """ASSET_PAUSED / ASSET_LIMITED carry the corporate action in reason_msg (earnings, split...)."""
        if self.reason_code in ("ASSET_PAUSED", "ASSET_LIMITED"):
            return self.reason_msg or "corporate action"
        return None


@dataclass(frozen=True)
class TokenSnapshot:
    token: TokenRef
    price: float | None               # on-chain token price, USD per token
    stock_price: float | None         # US stock feed price (may be null when the market is shut)
    multiplier: float
    status: AssetStatus | None
    holders: int | None = None
    week52_high: float | None = None
    week52_low: float | None = None
    fetched_ms: int = 0

    @property
    def per_share_price(self) -> float | None:
        if self.price is None or not self.multiplier:
            return None
        return self.price / self.multiplier
