"""The decision engine: one order in, ALLOW / REDUCE / BLOCK out, with every check explained.

Pipeline for a risk-increasing order (buy):

  1. data integrity     no price, stale quote, no reference while the market is shut  → BLOCK (fail closed)
  2. asset status       paused / halted / maintenance → BLOCK; earnings-limited → gap ×2.5, size ×0.5
  3. session phase      open / pre_close / closed / pre_open → position-cap multiplier; holiday weekends cut more
  4. risk per dollar    open: the stop distance; any closure phase: the 99 % weekend-gap quantile —
                        a stop-loss cannot fill at a price the stock market never prints
  5. stale reference    weekend drift vs. Friday close (relative to the gap quantile) and cross-issuer
                        dispersion → cut or block
  6. caps               position cap, gross-exposure ("leverage") cap, issuer notional limit, and the
                        account guards (static floor, daily loss limit, 40 % consistency)
  7. verdict            allowed = min(request, caps); BLOCK if any hard check fired or allowed < min order

Risk-reducing orders (sell) are never sized down: only data integrity and venue status can stop them.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .account import AccountState, Check, Holding, account_checks
from .policy import GuardPolicy
from .reference import MarketView

LIVE_SESSIONS = {"premarket", "regular", "postmarket", "overnight"}   # sessions with a live stock print
HARD_BLOCK_STATUS = {"ASSET_PAUSED", "MARKET_PAUSED", "MARKET_MAINTENANCE", "UNSUPPORTED"}
DEFAULT_GAP_Q99 = 0.08          # used for holdings/tickers with no gap model: deliberately pessimistic

PHASE_TEXT = {
    "open": "underlying market open — reference is live",
    "pre_close": "last hour before a multi-day closure",
    "closed": "underlying market closed — reference is frozen",
    "pre_open": "final 2 h before the reopen",
}


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str                       # "buy" adds risk, "sell" reduces it
    notional_usd: float
    stop_pct: float | None = None   # planned stop distance while the underlying trades

    def __post_init__(self):
        if self.side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        if not (self.notional_usd > 0 and math.isfinite(self.notional_usd)):
            raise ValueError("notional_usd must be a positive number")
        if self.stop_pct is not None and not (0 < self.stop_pct < 1):
            raise ValueError("stop_pct must be a fraction in (0, 1), e.g. 0.03 for 3 %")


@dataclass
class Decision:
    verdict: str                    # ALLOW | REDUCE | BLOCK
    order: OrderIntent
    allowed_usd: float
    at: datetime
    phase: str
    summary: str
    binding: str | None
    checks: list[Check]
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def size_ratio(self) -> float:
        return self.allowed_usd / self.order.notional_usd

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "symbol": self.order.symbol,
            "side": self.order.side,
            "requested_usd": round(self.order.notional_usd, 2),
            "allowed_usd": round(self.allowed_usd, 2),
            "size_ratio": round(self.size_ratio, 4),
            "at": self.at.isoformat(),
            "phase": self.phase,
            "summary": self.summary,
            "binding": self.binding,
            "checks": [c.to_dict() for c in self.checks],
            "metrics": _round_metrics(self.metrics),
        }

    def render(self) -> str:
        icon = {"ALLOW": "[ALLOW]", "REDUCE": "[REDUCE]", "BLOCK": "[BLOCK]"}[self.verdict]
        lines = [f"{icon} {self.order.side.upper()} {self.order.symbol} {self.order.notional_usd:,.2f} USD "
                 f"-> {self.allowed_usd:,.2f} USD   ({self.at:%a %Y-%m-%d %H:%M} UTC, {self.phase})",
                 f"  {self.summary}"]
        for c in self.checks:
            mark = {"pass": " ok ", "info": " .. ", "cut": " CUT", "block": " BLK"}[c.status]
            cap = f"  cap {c.cap_usd:,.0f}" if c.cap_usd is not None else ""
            lines.append(f"  [{mark}] {c.name:22s} {c.detail}{cap}")
        return "\n".join(lines)


def _round_metrics(m: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in m.items():
        if isinstance(v, float):
            out[k] = round(v, 6)
        elif isinstance(v, dict):
            out[k] = {kk: (round(vv, 6) if isinstance(vv, float) else vv) for kk, vv in v.items()}
        else:
            out[k] = v
    return out


def _age(sec: float | None) -> str:
    if sec is None:
        return "unknown"
    return f"{sec / 3600:.1f} h" if sec >= 7200 else f"{sec / 60:.0f} min"


def _pct(x: float | None, digits: int = 2) -> str:
    return "n/a" if x is None else f"{x * 100:+.{digits}f}%"


def effective_gap_q99(view: MarketView, policy: GuardPolicy) -> tuple[float, list[str]]:
    notes = []
    if view.gap is not None and view.gap.q99:
        q = view.gap.q99
        notes.append(f"q99 weekend gap {q:.2%} from {view.gap.n} observed weekends + σ model")
    else:
        q = DEFAULT_GAP_Q99
        notes.append(f"no gap history — pessimistic default q99 {q:.0%}")
    if view.status and view.status.reason_code == "ASSET_LIMITED":
        q *= 2.5
        notes.append("earnings/limited: ×2.5")
    c = view.closure
    if c.skipped_days > 2 and view.phase != "open":
        q *= math.sqrt(1 + 0.25 * (c.skipped_days - 2))
        notes.append(f"{c.kind} ({c.skipped_days} days shut): ×{math.sqrt(1 + 0.25 * (c.skipped_days - 2)):.2f}")
    if q < policy.gap_floor_pct:
        notes.append(f"floored at {policy.gap_floor_pct:.0%}")
        q = policy.gap_floor_pct
    return q, notes


def holding_planned_risk(h: Holding, phase: str, gap_by_ticker: dict[str, float], policy: GuardPolicy) -> float:
    """Planned (pre-slippage) loss a holding can produce before it can be exited."""
    q = gap_by_ticker.get(h.ticker, DEFAULT_GAP_Q99)
    if phase == "open":
        return abs(h.notional_usd) * (h.stop_pct or policy.default_stop_pct)
    return abs(h.notional_usd) * max(q, h.stop_pct or 0.0, policy.gap_floor_pct)


def evaluate(order: OrderIntent, view: MarketView, account: AccountState, policy: GuardPolicy | None = None,
             gap_by_ticker: dict[str, float] | None = None) -> Decision:
    policy = policy or GuardPolicy()
    gap_by_ticker = dict(gap_by_ticker or {})
    checks: list[Check] = []
    phase = view.phase
    metrics = view.metrics()
    hard_block = False

    # ---- 1. data integrity ----------------------------------------------------------------------
    if view.price is None or view.price <= 0:
        checks.append(Check("data.price", "block", "no on-chain price for this token — fail closed"))
        hard_block = True
    else:
        age = view.quote_age_s
        if age is None or age > policy.max_quote_age_s:
            checks.append(Check("data.price", "block",
                                f"on-chain quote is {_age(age)} old "
                                f"(max {policy.max_quote_age_s / 60:.0f} min) — no trades, no mark; fail closed",
                                data={"quote_age_s": age}))
            hard_block = True
        else:
            checks.append(Check("data.price", "pass",
                                f"{view.price:,.4f} USD/token (×{view.multiplier:.6g} sh), quote {_age(age)} old",
                                data={"quote_age_s": age}))

    for note in view.notes:
        checks.append(Check("data.note", "info", note))

    ref_age = view.reference_age_h
    if not view.regular_open:
        if view.reference_price is None:
            checks.append(Check("data.reference", "block",
                                "no reference close while the stock market is shut — cannot measure drift"))
            hard_block = True
        elif ref_age is not None and ref_age > policy.max_reference_age_h:
            checks.append(Check("data.reference", "block",
                                f"reference is {ref_age:.0f} h old (> {policy.max_reference_age_h:.0f} h) — longer "
                                f"than any scheduled closure; feed looks broken",
                                data={"reference_age_h": ref_age}))
            hard_block = True
        else:
            checks.append(Check("data.reference", "info",
                                f"reference = last regular close {view.reference_price:,.4f}/sh, "
                                f"{ref_age:.1f} h old", data={"reference_age_h": ref_age}))
    else:
        checks.append(Check("data.reference", "info", "regular session open — reference is live (age 0 h)"))

    # ---- 2. venue / asset status ---------------------------------------------------------------
    earnings = False
    st = view.status
    if st is not None and st.reason_code in HARD_BLOCK_STATUS:
        checks.append(Check("venue.status", "block",
                            f"{st.reason_code}{(': ' + st.reason_msg) if st.reason_msg else ''} — the venue itself "
                            f"is not trading this asset",
                            data={"reason_code": st.reason_code, "reason_msg": st.reason_msg}))
        hard_block = True
    elif st is not None and st.reason_code == "ASSET_LIMITED":
        earnings = True
        checks.append(Check("venue.status", "cut",
                            f"ASSET_LIMITED ({st.reason_msg or 'earnings'}) — gap risk ×2.5, size ×"
                            f"{policy.earnings_size_mult:g}", data={"reason_msg": st.reason_msg}))
    else:
        checks.append(Check("venue.status", "info",
                            f"{(st.reason_code or 'unknown') if st else 'unknown (replay: venue status not recorded)'}"
                            f"{(' / ' + st.market_status) if st and st.market_status else ''}"))

    # ---- sell side: risk-reducing, never sized down -----------------------------------------
    if order.side == "sell":
        if hard_block and any(c.name == "venue.status" and c.status == "block" for c in checks):
            return _finish(order, view, checks, 0.0, "BLOCK", "venue.status", metrics,
                           "Venue is not trading this asset; nothing can be sold right now.")
        if view.price is None:
            return _finish(order, view, checks, 0.0, "BLOCK", "data.price", metrics,
                           "No price: the guard will not sign off a blind exit.")
        note = ""
        if view.drift is not None and not view.regular_open:
            note = f" Note: you sell at a weekend price {_pct(view.drift)} vs the last close."
        checks.append(Check("side", "info", "risk-reducing order — size and exposure caps do not apply"))
        return _finish(order, view, checks, order.notional_usd, "ALLOW", None, metrics,
                       "Exits are never sized down." + note)

    # ---- 3. session phase ---------------------------------------------------------------------
    q99, q_notes = effective_gap_q99(view, policy)
    mult = policy.size_mult(phase)
    closure = view.closure
    ptext = PHASE_TEXT.get(phase, phase)
    if phase == "open" and not view.regular_open:
        ptext = "weekday overnight — no multi-day closure ahead, ordinary overnight risk"
    phase_detail = f"{ptext}; position cap ×{mult:g}"
    if phase != "open" and closure.skipped_days > 2:
        mult *= policy.long_closure_mult
        phase_detail += f", {closure.kind} ×{policy.long_closure_mult:g}"
    if phase != "open":
        hours_left = (closure.end - view.at).total_seconds() / 3600
        phase_detail += f"; reopen in {hours_left:.1f} h ({closure.end:%a %H:%M} UTC)"
    checks.append(Check("session.phase", "pass" if mult >= 1 else "cut", phase_detail,
                        data={"phase": phase, "mult": mult}))
    if earnings:
        mult *= policy.earnings_size_mult

    # ---- 4. risk per dollar --------------------------------------------------------------------
    stop = order.stop_pct or policy.default_stop_pct
    drift = view.drift
    shut = not view.regular_open and phase != "open"
    if phase == "open":
        move = stop
        move_detail = f"priced on the stop: {stop:.2%} (stop can fill while the market trades)"
    else:
        move = max(q99, stop)
        move_detail = (f"priced on the weekend gap, not the stop: {move:.2%} "
                       f"({'; '.join(q_notes)}) — a stop cannot fill at a price the market never prints")
        if shut and drift is not None and drift > 0:
            # You pay Friday × (1 + drift); the worst Monday print is Friday × (1 − q99).
            stale = 1 - (1 - q99) / (1 + drift)
            if stale > move:
                move = stale
                move_detail += (f"; buying {_pct(drift)} above Friday's close stretches the worst case to "
                                f"1 − (1 − q99)/(1 + drift) = {move:.2%}")
    worst_per_usd = move * policy.slippage_k
    checks.append(Check("risk.per_dollar", "info",
                        f"{move_detail}; worst fill ×{policy.slippage_k:g} → {worst_per_usd:.2%} of notional",
                        data={"move": move, "q99": q99, "stop": stop, "worst_per_usd": worst_per_usd}))
    metrics.update({"gap_q99_effective": q99, "risk_move": move, "worst_per_usd": worst_per_usd})

    # ---- 5. stale-reference signals ----------------------------------------------------------
    sigma = (view.gap.sigma_gap if view.gap is not None and view.gap.sigma_gap else q99 / 3.22)
    if shut and drift is not None and q99 > 0:
        z = abs(drift) / sigma if sigma else float("inf")
        chasing = drift > 0          # buying after the token already rose over the closure
        if abs(drift) >= policy.drift_block_ratio * q99 and chasing:
            checks.append(Check("reference.drift", "block",
                                f"token is {_pct(drift)} vs Friday close — beyond the q99 gap ({q99:.2%}); buying "
                                f"into a weekend move the stock market has not confirmed",
                                data={"drift": drift, "z": z}))
            hard_block = True
        elif z >= policy.drift_cut_sigma:
            mult *= 0.5
            checks.append(Check("reference.drift", "cut",
                                f"token is {_pct(drift)} vs Friday close = {z:.1f}σ of a weekend move "
                                f"(σ {sigma:.2%}) — the weekend price has wandered from the last real print, cap halved",
                                data={"drift": drift, "z": z}))
        else:
            checks.append(Check("reference.drift", "pass",
                                f"drift {_pct(drift)} vs Friday close ({z:.1f}σ, σ {sigma:.2%})",
                                data={"drift": drift, "z": z}))
    elif drift is not None:
        checks.append(Check("reference.drift", "info", f"{_pct(drift)} vs last regular close"))

    disp = view.dispersion
    if disp is not None:
        sib = ", ".join(f"{s} {p:,.2f}" for s, p in view.siblings.items())
        me = f"{view.per_share:,.2f}/sh"
        if disp >= policy.dispersion_block_pct:
            checks.append(Check("reference.dispersion", "block",
                                f"{me} is {disp:.2%} away from the cross-issuer median ({sib}) — this pool is "
                                f"not pricing the stock; no reliable mark", data={"dispersion": disp}))
            hard_block = True
        elif disp >= policy.dispersion_cut_pct:
            mult *= 0.5
            checks.append(Check("reference.dispersion", "cut",
                                f"{me} is {disp:.2%} away from the cross-issuer median ({sib}) — cap halved",
                                data={"dispersion": disp}))
        else:
            checks.append(Check("reference.dispersion", "pass",
                                f"{me} within {disp:.2%} of the cross-issuer median ({sib})", data={"dispersion": disp}))
    else:
        checks.append(Check("reference.dispersion", "info", "no live sibling issuer to cross-check against"))

    prem = view.premium
    live_print = view.regular_open or bool(view.status and view.status.market_status in LIVE_SESSIONS)
    if prem is not None and not live_print:
        checks.append(Check("reference.premium", "info",
                            f"stock print {view.stock_price:,.2f} is not a live session print for this token "
                            f"({'stock market closed' if phase != 'open' else 'outside the regular session'}) — not used"))
    elif prem is not None:
        if abs(prem) >= policy.dispersion_block_pct:
            checks.append(Check("reference.premium", "block",
                                f"token trades {_pct(prem)} vs the live stock print {view.stock_price:,.2f} — broken peg",
                                data={"premium": prem}))
            hard_block = True
        elif abs(prem) >= policy.dispersion_cut_pct:
            mult *= 0.5
            checks.append(Check("reference.premium", "cut",
                                f"token trades {_pct(prem)} vs the live stock print {view.stock_price:,.2f} — cap halved",
                                data={"premium": prem}))
        else:
            checks.append(Check("reference.premium", "pass",
                                f"{_pct(prem)} vs the live stock print {view.stock_price:,.2f}", data={"premium": prem}))

    # ---- 6. caps --------------------------------------------------------------------------------
    caps: dict[str, float] = {"request": order.notional_usd}

    pos_cap = policy.max_position_pct * account.equity * mult
    caps["position.cap"] = pos_cap
    checks.append(Check("position.cap", "pass" if pos_cap >= order.notional_usd else "cut",
                        f"{policy.max_position_pct:.0%} of equity {account.equity:,.0f} × size multiplier {mult:.3g}",
                        cap_usd=pos_cap, data={"mult": mult}))

    gross_x = policy.gross_cap_x(phase)
    gross_room = gross_x * account.equity - account.gross_exposure
    caps["exposure.gross"] = max(0.0, gross_room)
    checks.append(Check("exposure.gross", "pass" if gross_room >= order.notional_usd else ("block" if gross_room <= 0 else "cut"),
                        f"gross stock-token exposure {account.gross_exposure:,.0f} of {gross_x:g}× equity "
                        f"({gross_x * account.equity:,.0f}){' — spot-only, >1× only with borrowed funds' if gross_x > 1 else ''}",
                        cap_usd=max(0.0, gross_room), data={"gross": account.gross_exposure, "cap_x": gross_x}))

    if view.issuer_limit_usd:
        caps["issuer.limit"] = view.issuer_limit_usd
        checks.append(Check("issuer.limit", "pass" if view.issuer_limit_usd >= order.notional_usd else "cut",
                            f"issuer max active notional {view.issuer_limit_usd:,.0f} USD (limitInfo)",
                            cap_usd=view.issuer_limit_usd))

    gap_by_ticker.setdefault(view.token.ticker, q99)
    open_risk = sum(holding_planned_risk(h, phase, gap_by_ticker, policy) for h in account.holdings)
    acc_checks, max_new_risk, acc_mult = account_checks(account, open_risk, policy)
    checks.extend(acc_checks)
    if any(c.status == "block" for c in acc_checks):
        hard_block = True
    metrics["open_planned_risk"] = open_risk
    if max_new_risk is not None and move > 0 and not any(c.status == "block" for c in acc_checks):
        acc_cap = max(0.0, max_new_risk) / move * acc_mult
        caps["account"] = acc_cap
        metrics["max_new_planned_risk"] = max_new_risk
        checks.append(Check("account.cap", "pass" if acc_cap >= order.notional_usd else "cut",
                            f"new planned risk ≤ {max_new_risk:,.0f} USD at {move:.2%} per dollar"
                            f"{f' × {acc_mult:g}' if acc_mult != 1 else ''} → notional ≤ {acc_cap:,.0f}",
                            cap_usd=acc_cap, data={"max_new_risk": max_new_risk, "move": move}))

    # ---- 7. verdict ----------------------------------------------------------------------------
    binding = min(caps, key=lambda k: caps[k])
    allowed = max(0.0, min(caps.values()))
    allowed = math.floor(allowed * 100 + 1e-6) / 100          # cents, robust to 612.4999999
    if hard_block:
        blocker = next(c for c in checks if c.status == "block")
        return _finish(order, view, checks, 0.0, "BLOCK", blocker.name, metrics, _summary("BLOCK", blocker.name, checks, view, move))
    if allowed < policy.min_order_usd:
        return _finish(order, view, checks, 0.0, "BLOCK", binding, metrics,
                       _summary("BLOCK", binding, checks, view, move, allowed=allowed, min_order=policy.min_order_usd))
    if allowed + 1e-9 < order.notional_usd:
        return _finish(order, view, checks, allowed, "REDUCE", binding, metrics,
                       _summary("REDUCE", binding, checks, view, move, allowed=allowed))
    return _finish(order, view, checks, order.notional_usd, "ALLOW", None, metrics,
                   _summary("ALLOW", None, checks, view, move))


BINDING_TEXT = {
    "position.cap": "position cap for this session phase",
    "exposure.gross": "gross-exposure (leverage) cap",
    "issuer.limit": "issuer notional limit",
    "account": "account guards (static floor / daily loss limit / 40 % consistency)",
    "request": "your request",
}


def _summary(verdict: str, binding: str | None, checks: list[Check], view: MarketView, move: float,
             allowed: float | None = None, min_order: float | None = None) -> str:
    ctx = ""
    if view.phase == "pre_close":
        mins = (view.closure.start - view.at).total_seconds() / 60
        ctx = (f"The stock market shuts for {view.closure.hours:.0f} h in {mins:.0f} min; risk is priced on a "
               f"{move:.1%} gap, not on the stop. ")
    elif view.phase != "open" and view.reference_age_h is not None:
        ctx = (f"Reference price is {view.reference_age_h:.0f} h old; risk is priced on a {move:.1%} gap, "
               f"not on the stop. ")
    if verdict == "BLOCK":
        if min_order is not None:
            return f"{ctx}Largest safe size {allowed or 0:,.2f} USD is below the {min_order:,.0f} USD minimum ({BINDING_TEXT.get(binding or '', binding)})."
        c = next((c for c in checks if c.name == binding), None)
        return f"{ctx}Blocked by {binding}: {c.detail if c else ''}"
    if verdict == "REDUCE":
        return f"{ctx}Size cut to {allowed:,.2f} USD by the {BINDING_TEXT.get(binding or '', binding)}."
    return f"{ctx}Within every cap."


def _finish(order: OrderIntent, view: MarketView, checks: list[Check], allowed: float, verdict: str,
            binding: str | None, metrics: dict[str, Any], summary: str) -> Decision:
    return Decision(verdict=verdict, order=order, allowed_usd=allowed, at=view.at, phase=view.phase,
                    summary=summary, binding=binding, checks=checks, metrics=metrics)
