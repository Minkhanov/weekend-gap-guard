from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gapguard.market import calendar as cal  # noqa: E402
from gapguard.models import AssetStatus, TokenRef  # noqa: E402
from gapguard.risk.gap import GapModel  # noqa: E402
from gapguard.risk.reference import MarketView  # noqa: E402

UTC = timezone.utc
FIXTURES = ROOT / "tests" / "fixtures"

# Fixed instants around the weekend of Fri 9 Oct 2026 (US Eastern = UTC−4).
FRI_OPEN = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)        # 11:00 ET, regular session
FRI_PRE_CLOSE = datetime(2026, 10, 9, 19, 30, tzinfo=UTC)  # 15:30 ET, last hour
SAT_NOON = datetime(2026, 10, 10, 16, 0, tzinfo=UTC)       # Saturday 12:00 ET
MON_PRE_OPEN = datetime(2026, 10, 12, 12, 30, tzinfo=UTC)  # 08:30 ET
TUE_NIGHT = datetime(2026, 10, 13, 7, 0, tzinfo=UTC)       # 03:00 ET weekday overnight
XMAS_SAT = datetime(2026, 12, 26, 17, 0, tzinfo=UTC)       # long weekend (Christmas Friday)

TOKEN = TokenRef(chain_id="56", address="0x" + "11" * 20, symbol="TSLAon", ticker="TSLA", type_id=1)


def gap_model(q99: float = 0.08, n: int = 20) -> GapModel:
    return GapModel(ticker="TSLA", sigma_daily=q99 / 3.2, sigma_gap=q99 / 3.2, q95=q99 * 0.64, q99=q99,
                    q99_param=q99, observations=[], returns_used=200)


def make_view(at: datetime, *, price: float | None = 100.0, ref: float | None = 100.0, quote_age_s: float = 60,
              siblings: dict | None = None, status: AssetStatus | None = None, stock_price: float | None = None,
              q99: float | None = 0.08, limit: float | None = None, multiplier: float = 1.0,
              ref_at: datetime | None = None) -> MarketView:
    return MarketView(
        at=at, token=TOKEN, price=None if price is None else price * multiplier, multiplier=multiplier,
        quote_at=at - timedelta(seconds=quote_age_s), reference_price=ref,
        reference_at=ref_at or cal.last_close(at), status=status, stock_price=stock_price,
        siblings=siblings or {}, issuer_limit_usd=limit, gap=None if q99 is None else gap_model(q99), source="test")


@pytest.fixture
def view_factory():
    return make_view
