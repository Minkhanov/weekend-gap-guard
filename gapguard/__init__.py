"""Weekend Gap Guard — weekend gap-risk monitor for tokenized stocks on BNB Smart Chain.

Tokenized stocks (Ondo Global Markets, xStocks, Binance bStocks) keep trading on-chain while
the US stock market is closed. Monday's open can land far from Friday's close; positions and
loans collateralised with stock tokens absorb that jump at once. This package measures the
risk before the weekend, watches the weekend drift, tightens limits and alerts.

Read-only by design: public market data, public JSON-RPC reads, no keys with funds, no trading.
"""

__version__ = "0.1.0"
