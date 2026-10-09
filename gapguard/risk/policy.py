"""Guard policy: every threshold the engine uses, in one place, with its reason.

All numbers are explicit, overridable from a JSON file (`--policy my.json`, partial keys allowed) and
printed with every decision. Nothing is learned or hidden.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GuardPolicy:
    # --- position caps by session phase (fraction of equity a single new position may reach) ---------
    max_position_pct: float = 0.25          # regular session: one position ≤ 25 % of equity
    phase_size_mult: dict[str, float] = field(default_factory=lambda: {
        "open": 1.0,          # underlying market trading: reference is live
        "pre_close": 0.5,     # last hour before a multi-day closure: you are about to carry gap risk
        "closed": 0.35,       # weekend / holiday: reference frozen, stop-losses cannot protect the open
        "pre_open": 0.5,      # last 2 h before the reopen: the gap is about to print
    })
    long_closure_mult: float = 0.7          # extra cut per closure that skips > 2 days (holiday weekends)

    # --- "leverage": gross stock-token exposure ÷ equity (spot track: >1 only with borrowed stablecoins) -
    max_gross_x_open: float = 1.0
    max_gross_x_closed: float = 0.5         # applies to pre_close, closed and pre_open

    # --- worst-case loss model ----------------------------------------------------------------------
    default_stop_pct: float = 0.03          # regular-session risk when the order carries no stop
    slippage_k: float = 1.43                # worst stop fill vs. planned (gap-through + fees), as in our prop door
    gap_floor_pct: float = 0.02             # never price a weekend below a 2 % gap, however calm history was

    # --- stale-reference signals --------------------------------------------------------------------
    drift_cut_sigma: float = 1.0            # |weekend drift| ≥ 1 σ of a weekend move → halve the cap
    drift_block_ratio: float = 1.0          # upward drift ≥ q99 gap → no new buys (chasing an unconfirmed move)
    dispersion_cut_pct: float = 0.01        # issuers disagree by ≥ 1 % per share → halve the cap
    dispersion_block_pct: float = 0.03      # ≥ 3 % → no reliable mark, block new risk
    max_reference_age_h: float = 100.0      # older than the longest holiday closure → feed is broken

    # --- data integrity (fail-closed) ----------------------------------------------------------------
    max_quote_age_s: float = 900.0          # live: a price older than 15 min is not a price
    min_order_usd: float = 10.0

    # --- account guards (ported from our prop-trading gate, see docs) --------------------------------
    floor_reserve_usd: float = 0.0          # extra room kept above the static floor
    daily_reserve_usd: float = 0.0          # extra room kept inside the daily loss limit
    consistency_pct: float = 0.40           # one day's profit may not exceed 40 % of the profit target
    consistency_warn: float = 0.8           # at 80 % of that cap new risk is halved

    # --- earnings / corporate actions ----------------------------------------------------------------
    earnings_size_mult: float = 0.5

    def size_mult(self, phase: str) -> float:
        return float(self.phase_size_mult.get(phase, min(self.phase_size_mult.values())))

    def gross_cap_x(self, phase: str) -> float:
        return self.max_gross_x_open if phase == "open" else self.max_gross_x_closed

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "GuardPolicy":
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"unknown policy keys: {sorted(unknown)}")
        base = asdict(cls())
        if "phase_size_mult" in d:
            base["phase_size_mult"] = {**base["phase_size_mult"], **d["phase_size_mult"]}
            d = {k: v for k, v in d.items() if k != "phase_size_mult"}
        base.update(d)
        return cls(**base)

    @classmethod
    def load(cls, path: str | Path | None) -> "GuardPolicy":
        if not path:
            return cls()
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
