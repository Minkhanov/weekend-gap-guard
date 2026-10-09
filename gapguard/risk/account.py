"""Account-level guards, ported from the pre-trade gate of a live prop-trading account.

Three rules, all evaluated on EQUITY (unrealised P&L included):

  static floor      equity − k × (open planned risk + new planned risk) − reserve ≥ floor
                    "the stop of this trade must not be able to break the floor" — checked before the
                    order, not after the floor is already broken. The floor never trails.
  daily loss limit  drawdown_today + k × (open + new planned risk) + reserve ≤ daily limit
                    drawdown is measured from equity at the day reset, which is fixed for the day.
  consistency 40 %  if today's profit already reached 40 % of the profit target, no new risk today;
                    at 80 % of that cap new risk is halved.

`k` (slippage_k, 1.43 by default) turns planned risk into the worst fill we have actually seen:
gap-through plus fees. Planned risk itself is priced by the engine — on the stop distance while the
underlying market trades, and on the weekend-gap quantile while it is shut (a stop cannot fill at a
price the market never prints).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .policy import GuardPolicy

STATUS_RANK = {"info": 0, "pass": 0, "cut": 1, "block": 2}


@dataclass
class Check:
    name: str
    status: str                 # pass | cut | block | info
    detail: str
    cap_usd: float | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if d["cap_usd"] is not None:
            d["cap_usd"] = round(d["cap_usd"], 2)
        return d


@dataclass
class Holding:
    symbol: str
    ticker: str
    notional_usd: float
    stop_pct: float | None = None       # planned stop while the underlying trades; ignored over a closure


@dataclass
class AccountState:
    equity: float
    day_start_equity: float
    floor: float
    daily_loss_limit: float
    profit_target: float | None = None
    holdings: list[Holding] = field(default_factory=list)
    borrowed_usd: float = 0.0
    name: str = "account"

    @property
    def day_pnl(self) -> float:
        return self.equity - self.day_start_equity

    @property
    def drawdown_today(self) -> float:
        return max(0.0, self.day_start_equity - self.equity)

    @property
    def gross_exposure(self) -> float:
        return sum(abs(h.notional_usd) for h in self.holdings)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AccountState":
        d = dict(d)
        d["holdings"] = [Holding(**h) for h in d.get("holdings", [])]
        return cls(**d)

    @classmethod
    def load(cls, path: str | Path) -> "AccountState":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def demo_account() -> AccountState:
    """A 10 000 USD book shaped like a two-step prop evaluation: floor −10 %, daily limit −5 %, target +10 %."""
    return AccountState(equity=10_000.0, day_start_equity=10_000.0, floor=9_000.0, daily_loss_limit=500.0,
                        profit_target=1_000.0, name="demo 10k (floor 9 000, daily 500, target 1 000)")


def account_checks(acc: AccountState, open_planned_risk: float, policy: GuardPolicy
                   ) -> tuple[list[Check], float | None, float]:
    """Return (checks, max NEW planned risk in USD or None if unbounded, size multiplier).

    A `block` check means no new risk at all; the returned max risk is then 0.
    """
    k = policy.slippage_k
    checks: list[Check] = []
    mult = 1.0

    if acc.equity <= 0:
        checks.append(Check("account.equity", "block", "equity is zero or unknown — fail closed"))
        return checks, 0.0, 0.0

    # static floor ------------------------------------------------------------------------------
    room_floor = acc.equity - acc.floor - policy.floor_reserve_usd
    if room_floor <= 0:
        checks.append(Check("account.floor", "block",
                            f"equity {acc.equity:,.0f} is at/below the static floor {acc.floor:,.0f}",
                            data={"equity": acc.equity, "floor": acc.floor}))
        return checks, 0.0, 0.0
    max_risk_floor = room_floor / k - open_planned_risk
    worst_now = k * open_planned_risk
    if max_risk_floor <= 0:
        checks.append(Check("account.floor", "block",
                            f"open positions alone can lose {worst_now:,.0f} (worst case) — that already reaches "
                            f"the floor {acc.floor:,.0f} from equity {acc.equity:,.0f}",
                            data={"room": room_floor, "open_worst": worst_now}))
        return checks, 0.0, 0.0
    checks.append(Check("account.floor", "pass",
                        f"floor {acc.floor:,.0f}: room {room_floor:,.0f}; open worst case {worst_now:,.0f}; "
                        f"new planned risk ≤ {max_risk_floor:,.0f} (×{k:g} worst fill)",
                        data={"room": room_floor, "open_worst": worst_now, "max_new_risk": max_risk_floor}))

    # daily loss limit --------------------------------------------------------------------------
    dd = acc.drawdown_today
    room_day = acc.daily_loss_limit - dd - policy.daily_reserve_usd
    if room_day <= 0:
        checks.append(Check("account.daily", "block",
                            f"today's drawdown {dd:,.0f} reached the daily limit {acc.daily_loss_limit:,.0f}",
                            data={"drawdown": dd, "limit": acc.daily_loss_limit}))
        return checks, 0.0, 0.0
    max_risk_day = room_day / k - open_planned_risk
    if max_risk_day <= 0:
        checks.append(Check("account.daily", "block",
                            f"drawdown {dd:,.0f} + open worst case {worst_now:,.0f} already fill the daily limit "
                            f"{acc.daily_loss_limit:,.0f}",
                            data={"drawdown": dd, "open_worst": worst_now}))
        return checks, 0.0, 0.0
    checks.append(Check("account.daily", "pass",
                        f"daily limit {acc.daily_loss_limit:,.0f}: drawdown {dd:,.0f}; new planned risk ≤ "
                        f"{max_risk_day:,.0f}",
                        data={"drawdown": dd, "max_new_risk": max_risk_day}))

    # 40 % consistency --------------------------------------------------------------------------
    if acc.profit_target:
        cap = policy.consistency_pct * acc.profit_target
        pnl = acc.day_pnl
        if pnl >= cap:
            checks.append(Check("account.consistency", "block",
                                f"today's profit {pnl:,.0f} reached {policy.consistency_pct:.0%} of the target "
                                f"({cap:,.0f}) — more risk today breaks the consistency rule",
                                data={"day_pnl": pnl, "cap": cap}))
            return checks, 0.0, 0.0
        if pnl >= policy.consistency_warn * cap:
            mult = 0.5
            checks.append(Check("account.consistency", "cut",
                                f"today's profit {pnl:,.0f} is ≥ {policy.consistency_warn:.0%} of the "
                                f"{policy.consistency_pct:.0%} cap ({cap:,.0f}) — new size halved",
                                data={"day_pnl": pnl, "cap": cap}))
        else:
            checks.append(Check("account.consistency", "pass",
                                f"today's P&L {pnl:+,.0f} vs consistency cap {cap:,.0f}",
                                data={"day_pnl": pnl, "cap": cap}))
    else:
        checks.append(Check("account.consistency", "info", "no profit target set — consistency rule off"))

    return checks, min(max_risk_floor, max_risk_day), mult
