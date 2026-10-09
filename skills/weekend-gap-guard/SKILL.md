---
name: weekend-gap-guard
description: |
  Pre-trade risk gate for tokenized US stocks on BNB Smart Chain (Ondo "…on", xStocks "…x", bStocks "…B").
  Run it BEFORE any buy or sell of a stock token through the binance-agentic-wallet skill (`baw`).
  It answers ALLOW / REDUCE / BLOCK with the size it allows and a reason for every check:
  stale reference price over weekends and holidays, weekend drift vs Friday's close, issuers that
  disagree with each other, stale or broken pools, venue pauses and earnings limits, position and
  exposure caps, and account guards (static floor, daily loss limit, 40 % consistency rule).

  Use when the user asks to buy, sell, size or check a tokenized stock, asks "is it safe to buy TSLA
  token now / on the weekend", or when an automated strategy is about to place a stock-token order.
  NOT for crypto tokens (BTC, BNB, memecoins) and NOT an execution tool: it never signs or sends.
metadata:
  author: weekend-gap-guard
  version: "0.1.0"
  requires:
    bins: [python>=3.11]
  composes:
    - binance-web3/binance-agentic-wallet        # execution (baw CLI) — optional
    - binance-web3/binance-tokenized-securities-info   # same public endpoints the guard reads
---

# Weekend Gap Guard

The stock market prints nothing from Friday 16:00 ET to Monday 09:30 ET (longer on holidays), but stock
tokens keep trading on-chain. A stop-loss cannot fill at a price the market never prints, so weekend
risk must be priced on the Monday gap, not on the stop. This skill puts that rule — and the rest of a
professional pre-trade gate — in front of the wallet.

## Install

```bash
git clone https://github.com/Minkhanov/weekend-gap-guard && cd weekend-gap-guard
python -m pip install -e .          # deps: httpx, tzdata
gapguard --help
```

No API key, no account, no wallet: data comes from the public Binance Wallet Skills RWA endpoints and
public BSC JSON-RPC.

## The one command

```bash
gapguard check <SYMBOL> --usd <NOTIONAL> [--side buy|sell] [--stop 0.03] [--account account.json] \
               --json --wallet-plan --exit-code
```

- `<SYMBOL>` is the exact token symbol: `TSLAon` (Ondo), `TSLAx` (xStocks), `TSLAB` (bStocks).
  If the user names only a ticker ("buy Tesla token"), ask which issuer — do not guess.
- `--account` is the user's own risk book (equity, day-start equity, static floor, daily loss limit,
  profit target, current holdings). Without it a demo 10 000 USD book is used — say so to the user.
- Exit code: `0` ALLOW/REDUCE, `3` BLOCK, `2` data source error.

### Output contract (JSON)

| Field | Meaning |
|---|---|
| `verdict` | `ALLOW`, `REDUCE` or `BLOCK` |
| `requested_usd` / `allowed_usd` | what was asked / what the guard allows |
| `binding` | the check that set the size (`position.cap`, `exposure.gross`, `issuer.limit`, `account`, or the blocking check) |
| `summary` | one sentence to show the user verbatim |
| `checks[]` | `{name, status: pass/cut/block/info, detail, cap_usd}` — every check, including the passing ones |
| `metrics` | phase, reference price and age, drift, dispersion, q99 gap, risk per dollar |
| `agentic_wallet` | `{execute: false, quote_cmd, swap_cmd, from_qty, slippage_pct, reason}` |

## Rules for the agent

1. **Always run the guard first** for any stock-token order, including when the user is in a hurry.
2. **BLOCK → do not call `baw`.** Show `summary` and the blocking check's `detail`. Offer to re-check later
   or to choose another issuer of the same stock (the board often shows one issuer blocked and another fine).
3. **REDUCE → use `allowed_usd`, never the original amount.** Tell the user both numbers and the `binding` reason.
4. **Quote before swap.** Run `agentic_wallet.quote_cmd`, show the quote together with the guard's summary,
   and run `swap_cmd` only after an explicit "yes" — exactly as the binance-agentic-wallet skill requires.
   Keep the guard's `--slippage` cap; do not replace it with `auto` while the stock market is shut.
5. **Sells are never sized down.** A sell is only blocked when the venue is paused or there is no price.
6. **Fail closed.** If the guard cannot read prices (exit code 2), do not trade on stale numbers.
7. Do not present the guard's output as investment advice; it is a risk limit, not a signal.

## Other commands an agent may use

```bash
gapguard status                         # session clock: open / pre_close / closed / pre_open, next reopen
gapguard portfolio 0xADDRESS            # watch-only: stock tokens held on BSC + weekend loss at q99
gapguard gaps TSLA NVDA                 # how big Monday gaps have been, and how much the weekend priced in
gapguard check MSTRon --usd 2000 --at 2026-09-20T16:00Z   # replay a past moment from recorded data
```

## Example

User: "Buy $2,000 of TSLAB, it's Saturday."

```bash
gapguard check TSLAB --usd 2000 --json --wallet-plan --exit-code
```

→ `REDUCE 2000 → 875`, binding `position.cap`: "Reference price is 20 h old; risk is priced on a 9.6 % gap,
not on the stop." The agent shows that, then runs
`baw market-order quote --fromTokenQty 875 --fromToken 0x55d3…7955 --toToken 0x5b19…292f --binanceChainId 56 --slippage 1 --json`,
shows the quote, and waits for the user's confirmation before `market-order swap`.
