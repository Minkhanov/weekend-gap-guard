"""Watch-only portfolio: which stock tokens an address holds on BSC, and what a weekend gap could cost.

Balances come straight from the chain (`balanceOf` via public JSON-RPC, batched) — no key, no
signature, no account. Prices come from the public RWA Dynamic V2 endpoint.
"""
from __future__ import annotations

from typing import Any

from .collect import ISSUER_TYPES
from .risk.engine import DEFAULT_GAP_Q99
from .sources.bsc_rpc import BscRpc
from .sources.binance_rwa import RwaClient
from .views import HistoryBook

_f = lambda v: None if v in (None, "") else float(v)


def scan(client: RwaClient, rpc: BscRpc, owner: str, book: HistoryBook | None = None) -> dict[str, Any]:
    tokens = [t for t in client.tokens() if t.type_id in ISSUER_TYPES]
    bals = rpc.balances(owner, tokens)
    by_sym = {t.symbol: t for t in tokens}
    rows = []
    for sym, amount in sorted(bals.items()):
        t = by_sym[sym]
        dyn = client.raw("dynamic", {"chainId": t.chain_id, "contractAddress": t.address}) or {}
        price = _f((dyn.get("tokenInfo") or {}).get("price"))
        q99 = None
        if book is not None and t.ticker in book.by_ticker:
            from datetime import datetime, timezone
            m = book.gap_model(t.ticker, datetime.now(timezone.utc))
            q99 = m.q99 if m else None
        q = q99 or DEFAULT_GAP_Q99
        usd = amount * price if price else None
        rows.append({"symbol": sym, "ticker": t.ticker, "provider": t.provider, "address": t.address,
                     "amount": amount, "price": price, "usd": usd, "gap_q99": q,
                     "gap_q99_source": "history" if q99 else "default",
                     "weekend_loss_q99": usd * q if usd else None})
    total = sum(r["usd"] or 0 for r in rows)
    loss = sum(r["weekend_loss_q99"] or 0 for r in rows)
    return {"owner": owner, "tokens_scanned": len(tokens), "holdings": rows, "total_usd": total,
            "weekend_loss_q99_usd": loss, "rpc": rpc.url}
