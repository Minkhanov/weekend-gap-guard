# Weekend Gap Guard

**A pre-trade risk gate for tokenized stocks on BNB Chain — what to allow, cut or block while the stock market is closed.**

Dashboard: https://minkhanov.github.io/weekend-gap-guard/ · Demo video (2 min, captions): https://minkhanov.github.io/weekend-gap-guard/demo.mp4 ·
Developer Experience Report: [`docs/DEVELOPER_EXPERIENCE_REPORT.md`](docs/DEVELOPER_EXPERIENCE_REPORT.md) ·
Agent Skill: [`skills/weekend-gap-guard/SKILL.md`](skills/weekend-gap-guard/SKILL.md)

Built for **BNB Hack: Tokenized Stocks Edition** (main track). Works on BSC mainnet data for all three issuers —
**Ondo** (`…on`), **xStocks** (`…x`), **bStocks** (`…B`) — through public, key-less endpoints.

---

## The problem

From Friday 16:00 ET to Monday 09:30 ET the US stock market prints nothing. Tokenized stocks on BSC keep
trading. Everything an agent normally relies on breaks quietly:

- **The reference price freezes.** For 65.5 hours (89.5 on a holiday weekend) the last real price of the stock is Friday's close.
- **A stop-loss stops protecting you.** Monday's open can land anywhere; a stop cannot fill at a price the market never prints.
  In our record the largest Friday→Monday gaps were +8.9 % (MSTR), +11.8 % (CRCL), +7.6 % (COIN).
- **The weekend on-chain price is not a mark.** Since Ondo started weekend trading on BSC (June 2026), the Sunday-night price
  had already shown less than 10 % of Monday's move for most single stocks (TSLA +4 %, NVDA −14 %, AAPL −4 %, COIN −1 %;
  AMZN was the exception at +21 %) and about a quarter for SPY/QQQ — on 9–16 weekends per ticker, so small samples.
- **Issuers disagree.** On 9 Oct 2026, 07:08 UTC the xStocks pools on BSC quoted META at 572.44 per share against 722.35 (Ondo)
  and 722.70 (bStocks); 7 of the 10 xStocks tokens we track had not traded for 24 h. Their hourly K-lines contain prints like
  AAPLx at 36 million USD.

An agent that sizes weekend trades like weekday trades is taking a risk it cannot see.

## What it does

Every order goes through one function and comes back **ALLOW / REDUCE / BLOCK** with the size it allows and a reason
for every check — the passing ones too.

```text
$ gapguard check TSLAB --usd 2000 --at 2026-10-03T16:00Z --wallet-plan
[REDUCE] BUY TSLAB 2,000.00 USD -> 875.00 USD   (Sat 2026-10-03 16:00 UTC, closed)
  Reference price is 20 h old; risk is priced on a 9.5% gap, not on the stop. Size cut to 875.00 USD by the position cap for this session phase.
  [ ok ] data.price             369.9877 USD/token (×1 sh), quote 0 min old
  [ .. ] data.reference         reference = last regular close 370.6750/sh, 20.0 h old
  [ .. ] venue.status           unknown (replay: venue status not recorded)
  [ CUT] session.phase          underlying market closed — reference is frozen; position cap ×0.35; reopen in 45.5 h (Mon 13:30 UTC)
  [ .. ] risk.per_dollar        priced on the weekend gap, not the stop: 9.49% (q99 weekend gap 9.49% from 57 observed weekends + σ model) — a stop cannot fill at a price the market never prints; worst fill ×1.43 → 13.58% of notional
  [ ok ] reference.drift        drift -0.19% vs Friday close (0.1σ, σ 2.95%)
  [ ok ] reference.dispersion   369.99/sh within 0.13% of the cross-issuer median (TSLAon 370.94)
  [ CUT] position.cap           25% of equity 10,000 × size multiplier 0.35  cap 875
  [ ok ] exposure.gross         gross stock-token exposure 0 of 0.5× equity (5,000)  cap 5,000
  [ ok ] account.floor          floor 9,000: room 1,000; open worst case 0; new planned risk ≤ 699 (×1.43 worst fill)
  [ ok ] account.daily          daily limit 500: drawdown 0; new planned risk ≤ 350
  [ ok ] account.consistency    today's P&L +0 vs consistency cap 400
  [ ok ] account.cap            new planned risk ≤ 350 USD at 9.49% per dollar → notional ≤ 3,683  cap 3,683
  source: replay:history-1h

  Agentic Wallet hand-off (not executed): REDUCE: 2,000.00 → 875.00 USD. Quote first; run the swap only after the user confirms (wallet skill rule). Slippage capped at 1% (closed).
    baw market-order quote --fromTokenQty 875 --fromToken 0x55d398326f99059fF775485246999027B3197955 --toToken 0x5b1910eaad6450e50f816082aa078c41f10c292f --binanceChainId 56 --slippage 1 --json
    baw market-order swap --fromTokenQty 875 --fromToken 0x55d398326f99059fF775485246999027B3197955 --toToken 0x5b1910eaad6450e50f816082aa078c41f10c292f --binanceChainId 56 --slippage 1 --json
```

### The pipeline

| # | Stage | What it does |
|---|---|---|
| 1 | Data integrity | No price, a quote older than 15 min, a pool with no trade for 24 h, or no reference while the market is shut → **BLOCK**. Fail closed. |
| 2 | Venue status | `ASSET_PAUSED`, `MARKET_PAUSED`, maintenance → **BLOCK**. `ASSET_LIMITED` (earnings) → gap ×2.5, size ×0.5. |
| 3 | Session phase | NYSE calendar 2026–27 (holidays, early closes, DST): `open` · `pre_close` (last hour) · `closed` · `pre_open` (last 2 h). Position cap ×1 / ×0.5 / ×0.35 / ×0.5; holiday weekends ×0.7 more. |
| 4 | Risk per dollar | While the stock trades: the stop. While it is shut: the **99 % weekend-gap quantile**, stretched to `1 − (1 − q99)/(1 + drift)` when you buy above Friday's close. ×1.43 for the worst fill we have measured (gap-through + fees). |
| 5 | Stale reference | Weekend drift vs Friday close in σ of a weekend move (≥ 1σ halves the cap; buying a rally beyond q99 is blocked); distance from the **cross-issuer median** (≥ 1 % halves, ≥ 3 % blocks); premium to a live stock print. |
| 6 | Caps | Position cap by phase; gross-exposure ("leverage") cap 1.0× → 0.5× of equity over closures; issuer notional limit (`limitInfo.maxActiveNotionalValue`); account guards — **static floor**, **daily loss limit**, **40 % consistency rule**. |
| 7 | Verdict | `allowed = min(request, caps)`; BLOCK below 10 USD. **Exits are never sized down.** |

The account guards are ported from the pre-trade gate of a live prop-trading account: "the stop of this trade must not
be able to break the floor" — checked before the order, on equity, with the floor never trailing. Here the same door is
priced on the weekend gap instead of the stop.

Every threshold lives in one dataclass ([`gapguard/risk/policy.py`](gapguard/risk/policy.py)), is printed with the
decision, and can be overridden from JSON (`--policy examples/policy_strict_weekend.json`).

## Architecture

```mermaid
flowchart LR
  subgraph Public data — no key
    A[Binance Wallet Skills RWA API<br/>token list · market status · asset status<br/>RWA Dynamic V2 · Token K-Line]
    B[BSC JSON-RPC<br/>eth_call balanceOf / totalSupply]
  end
  A --> V[views.py<br/>MarketView at time t<br/>live · recorded · replay]
  H[(data/history<br/>hourly candles)] --> V
  L[(data/live<br/>weekend recordings)] --> V
  V --> E[risk/engine.py<br/>ALLOW · REDUCE · BLOCK<br/>+ explained checks]
  G[risk/gap.py<br/>weekend-gap model] --> E
  C[market/calendar.py<br/>NYSE sessions] --> E
  P[risk/account.py<br/>floor · daily · 40 %] --> E
  E --> CLI[gapguard CLI · JSON]
  E --> W[integrations/agentic_wallet.py<br/>baw quote / swap command<br/>never executed]
  E --> S[site.py → docs/ dashboard]
  B --> PF[portfolio.py<br/>watch-only exposure]
  CLI --> SK[Agent Skill<br/>skills/weekend-gap-guard]
```

## What we use from Binance Web3 / BNB Chain

| Piece | How | Status |
|---|---|---|
| Wallet Skills RWA endpoints (`binance-tokenized-securities-info`): token list, market status, asset status, RWA Dynamic V2, Token K-Line | prices, multipliers, issuer limits, session status, hourly history | **live**, public, no key |
| BSC mainnet JSON-RPC (`bsc-dataseed.bnbchain.org`) | `balanceOf` for watch-only portfolios; `totalSupply`/`decimals` reconciled against the API | **live**, public |
| Agentic Wallet (`binance-agentic-wallet` skill v1.12.0, CLI `baw` 1.10.0) | the guard emits `baw market-order quote/swap` with the allowed size and a slippage cap | **syntax verified** against the real `baw` 1.10.0 parser (it stops at `NOT_LOGGED_IN`); **not executed** — signing in needs a Binance account and the Binance app |
| Agent Skill format | `skills/weekend-gap-guard/SKILL.md` composes the two Binance skills above | ready |
| Binance Web3 API (signed) | `gapguard/sources/web3_api.py` implements the documented HMAC signing (unit-tested) | **not verified live** — no Developer Portal key (see DX report) |
| Transaction API dry-run, b402 / x402, BNB Agent Studio | — | not used |

## Run it (judges: ~2 minutes)

```bash
git clone https://github.com/Minkhanov/weekend-gap-guard && cd weekend-gap-guard
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest -q                                           # 80 offline tests (+1 opt-in against the real baw CLI)

# live, public endpoints (no key)
gapguard status
gapguard check TSLAB --usd 2000 --wallet-plan
gapguard check METAx --usd 2000                     # on 9 Oct 2026: stale, 20 % off-median pool → BLOCK

# replay real weekends from recorded data (offline)
gapguard check MSTRon --usd 2000 --at 2026-09-21T12:00Z
gapguard scenario TSLA --friday 2026-10-02
gapguard gaps                                       # out-of-sample calibration, 540 ticker-weekends
gapguard stress                                     # Saturday buys vs Monday open, guarded vs raw

# watch-only portfolio on BSC
gapguard portfolio 0x<any BSC address>

# as a library inside an agent
python examples/agent_pretrade.py TSLAB 2000
```

Dashboard locally: `gapguard site && python -m http.server -d docs 8000`. Data refresh: `gapguard history`
(hourly candles, ~280 requests at 2 req/s) and `gapguard record --every 600 --until 2026-10-11T12:00Z` (weekend poller).

## Evidence

**Out-of-sample calibration.** For every weekend, the gap model is fitted on data before Friday's close and compared
with Monday 10:00 ET. 10 tickers, Ondo tokens on BSC, Sep 2025 → Oct 2026:

| Ticker | Weekends | Mean \|gap\| | Max \|gap\| | Median q99 | > q99 | Weekends with on-chain trading | Priced in by Sunday |
|---|---|---|---|---|---|---|---|
| AAPL | 54 | 0.95 % | 2.61 % | 4.63 % | 0 | 11 | −4 % |
| AMZN | 54 | 1.38 % | 4.85 % | 6.41 % | 0 | 9 | +21 % |
| COIN | 54 | 2.48 % | 7.56 % | 14.08 % | 0 | 9 | −1 % |
| CRCL | 54 | 3.48 % | 11.84 % | 19.54 % | 0 | 16 | +5 % |
| META | 54 | 1.48 % | 7.01 % | 7.35 % | 0 | 11 | +8 % |
| MSTR | 54 | 2.92 % | 8.86 % | 15.38 % | 0 | 9 | +10 % |
| NVDA | 54 | 1.40 % | 3.67 % | 7.43 % | 0 | 16 | −14 % |
| QQQ | 54 | 0.76 % | 2.49 % | 3.51 % | 0 | 16 | +21 % |
| SPY | 54 | 0.48 % | 1.83 % | 2.54 % | 0 | 16 | +25 % |
| TSLA | 54 | 2.14 % | 5.27 % | 9.61 % | 0 | 16 | +4 % |

0 of 540 Monday gaps exceeded the q99 the guard would have used. That is **conservative by design** (σ of daily closes ×
2.576 × 1.25, never below the largest gap already seen); the cost is that weekends are sized more cautiously than strictly
necessary, and `fat_tail` in the policy is the knob to tighten it.

**Stress.** Buy 5,000 USD of the Ondo token at Saturday 12:00 ET on every weekend that had on-chain trading (129 real
weekends), mark at Monday 10:00 ET: worst result −217.69 USD raw vs **−38.10 USD guarded**; 5 % tail −176.00 vs −26.76.
The mean also falls (+7.12 → +1.31): the guard is a risk limit, not an alpha claim.

## What is real and what is not

- **Real:** all prices, candles, statuses, multipliers, issuer limits and addresses come from the public Binance Wallet
  Skills endpoints for BSC (chain 56); balances from public BSC JSON-RPC. Hourly history is stored in `data/history/`
  (as fetched, timestamps included); the weekend of 9–11 Oct 2026 is being recorded live into `data/live/`.
- **Replays** use hourly candles: a candle that closed ≤ 2 h before the decision counts as a live quote (live mode: 15 min).
  Historical venue status (pauses, earnings) is not served by the endpoints, so replays mark it "unknown".
- **Reference close** = the Ondo token's hourly close at 16:00 ET (Ondo tracks the stock within ~0.1 % in regular hours, per
  the Binance skill docs) — not an exchange feed. Multipliers are taken as constant inside a replayed weekend.
- **No trades.** Nothing in this repo signs or broadcasts. The Agentic Wallet hand-off prints commands; executing them needs a
  Binance account + Binance app (Agentic Wallet prerequisite), which our team does not use. The hackathon's "demo with small
  live amounts" step was therefore not performed; the guard is scored on build quality, and the hand-off is the integration point.
- **Demo account** (10,000 USD, floor 9,000, daily limit 500, target 1,000) is illustrative; pass your own with `--account`.

## Repository layout

```
gapguard/
  cli.py                  command line (check, status, scenario, gaps, stress, portfolio, history, record, site)
  risk/engine.py          the decision pipeline
  risk/account.py         static floor, daily loss limit, 40 % consistency
  risk/gap.py             weekend-gap model (σ of closes, empirical max, earnings/holiday scaling)
  risk/reference.py       MarketView: reference age, drift, cross-issuer dispersion, premium
  risk/policy.py          every threshold, overridable from JSON
  market/calendar.py      NYSE sessions, holidays and early closes 2026–27, DST-correct
  views.py                MarketView from live API, recorded cycles or hourly history — no look-ahead
  scenarios.py            weekend replay, out-of-sample calibration, stress
  sources/                public RWA client (record/replay), BSC JSON-RPC, signed Web3 API client (unverified)
  integrations/agentic_wallet.py   baw command hand-off (formatting only)
  portfolio.py            watch-only BSC portfolio + weekend exposure
  site.py                 builds docs/data for the dashboard
skills/weekend-gap-guard/SKILL.md   Agent Skill
docs/                     dashboard (GitHub Pages), DX report, demo video
data/                     recorded hourly history and live weekend recordings
tests/                    81 tests (pytest): 80 offline + 1 opt-in against the real baw CLI
```

## License

MIT — see [LICENSE](LICENSE).
