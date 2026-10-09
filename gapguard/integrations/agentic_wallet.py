"""Binance Agentic Wallet hand-off: turn a guard decision into the exact `baw` command — never run it.

The guard sits *in front of* the execution layer. An agent that uses the official
`binance-agentic-wallet` skill (CLI `baw`, skill v1.12.0, CLI ≥ 1.10.0) calls the guard first and only
then builds the order — with the size the guard allowed, an explicit slippage cap instead of "auto"
when the reference is frozen, and the guard's explanation to show the user before the confirmation
the wallet skill requires anyway.

Command syntax follows binance-skills-hub/skills/binance-web3/binance-agentic-wallet/references/
market-order.md (fetched 2026-10-09). This module only formats strings; it never spawns a process.
"""
from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any

from ..risk.engine import Decision

BSC_CHAIN = "56"
PAY_TOKENS = {                                   # from the Agentic Wallet skill's Common Token Addresses (BSC)
    "USDT": "0x55d398326f99059fF775485246999027B3197955",
    "USDC": "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
    "USD1": "0x8d0D000Ee44948FC98c9B98A4FA4921476f08B0d",
}
SLIPPAGE_PCT = {"open": 0.5, "pre_close": 0.75, "closed": 1.0, "pre_open": 1.0}


@dataclass
class WalletPlan:
    execute: bool                 # always False from this module: a human/agent confirms in the wallet skill
    verdict: str
    reason: str
    quote_cmd: str | None
    swap_cmd: str | None
    from_qty: str | None
    slippage_pct: float | None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _qty(x: float, decimals: int) -> str:
    s = f"{x:.{decimals}f}".rstrip("0").rstrip(".")
    return s or "0"


def plan(decision: Decision, token_address: str, token_price: float | None, *, pay: str = "USDT") -> WalletPlan:
    pay = pay.upper()
    if pay not in PAY_TOKENS:
        raise ValueError(f"pay token must be one of {sorted(PAY_TOKENS)}")
    if decision.verdict == "BLOCK" or decision.allowed_usd <= 0:
        return WalletPlan(False, decision.verdict, f"guard blocked: {decision.summary}", None, None, None, None)
    slip = SLIPPAGE_PCT.get(decision.phase, 1.0)
    if decision.order.side == "buy":
        from_token, to_token = PAY_TOKENS[pay], token_address
        qty = _qty(decision.allowed_usd, 2)                      # stablecoin amount ≈ USD
    else:
        if not token_price:
            return WalletPlan(False, decision.verdict, "no token price — cannot convert USD to token quantity",
                              None, None, None, None)
        from_token, to_token = token_address, PAY_TOKENS[pay]
        qty = _qty(decision.allowed_usd / token_price, 6)
    base = ["--fromTokenQty", qty, "--fromToken", from_token, "--toToken", to_token,
            "--binanceChainId", BSC_CHAIN, "--slippage", f"{slip:g}"]
    quote = shlex.join(["baw", "market-order", "quote", *base, "--json"])
    swap = shlex.join(["baw", "market-order", "swap", *base, "--json"])
    reason = (f"{decision.verdict}: {decision.order.notional_usd:,.2f} → {decision.allowed_usd:,.2f} USD. "
              f"Quote first; run the swap only after the user confirms (wallet skill rule). "
              f"Slippage capped at {slip:g}% ({decision.phase}).")
    return WalletPlan(False, decision.verdict, reason, quote, swap, qty, slip)
