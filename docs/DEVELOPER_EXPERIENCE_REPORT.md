# Developer Experience Report — Weekend Gap Guard

BNB Hack: Tokenized Stocks Edition · solo team · built 5–10 Oct 2026 · repo: https://github.com/Minkhanov/weekend-gap-guard

This report follows the seven sections of the official DX form. Every item names the page or endpoint, what we sent,
what came back, and when. Times are UTC. Versions: Python 3.12.10, httpx 0.28.1, Node 22.20.0, npm 10.9.3,
`@binance/agentic-wallet` (`baw`) 1.10.0, skills `binance-tokenized-securities-info` v1.1 and `binance-agentic-wallet`
v1.12.0 (hub commit `9960c675`, 2026-09-11).

**What we actually called:** the public Wallet Skills RWA endpoints (token list, market status, asset status, RWA
Dynamic V2, Token K-Line) — a few hundred requests a day while building, plus a weekend poller (31 requests every 10 minutes); public BSC JSON-RPC (`eth_call`, `eth_blockNumber`);
the `baw` CLI, read-only commands only. **What we could not call:** the signed Binance Web3 API (no Developer Portal key),
the Transaction API dry-run, b402, BNB Agent Studio.

---

## 1. Onboarding

**First successful call: under 15 minutes.** The `curl` example in the `binance-tokenized-securities-info` SKILL.md
(API 1, Token Symbol List) worked as written on the first try — no key, plain GET, `Accept-Encoding: identity`.

**Working API key: never obtained.**

1. https://web3.binance.com/en/dev-portal → **Get Started** → a "Log In" dialog with two tabs, *Login Binance* and
   *Connect Wallet*. There is no email or GitHub option.
2. *Connect Wallet* tab immediately shows: *"Wallet login is not available in your region. Based on your IP address this
   service cannot be provided."* with a link to the prohibited-regions page (2026-10-09 ~07:00 UTC).
3. The check is IP-based. That is the rule working as designed — but a developer behind a corporate network or a
   cloud egress cannot even see what the portal offers, the message does not say which region was detected, and the
   *Login Binance* tab shows no warning at all.
4. The other route, *Login Binance*, requires a Binance account. We do not use one, so there was no path to a key for us.

**Where we got stuck:** the Introduction page (https://web3.binance.com/en/dev-docs/introduction) lists the five modules
but has no link to the Developer Portal, no base URL and no "how to get access" step; the Authentication page says
"go to the Developer Portal" without saying which login methods exist or that login is IP-geo-checked (that sentence is
only on the prohibited-regions page: "IP checks are enforced on both the Developer Portal and the API server side").

**Step that took longer than expected:** working out *which* API the hackathon means. The page lists "RWA Data, Market,
Trading, Transaction…" modules of the signed Web3 API, while the Skills Hub serves the same tokenized-stock data from
public `www.binance.com/bapi/defi/...` endpoints with no key. Nothing on either page says that these are the same data,
which one judges expect, or whether the public one is stable enough to build on.

**llms.txt:** yes, we fed `llms.txt` / `.md` pages to an AI coding agent. Navigation was useful. But **every docs URL,
including llms.txt itself, answers non-browser clients with `HTTP 202`, an empty body and `x-amzn-waf-action: challenge`**:

```bash
curl -s -o /dev/null -D - https://web3.binance.com/en/dev-docs/llms.txt | grep -i "HTTP/\|waf"
# HTTP/1.1 202 Accepted
# x-amzn-waf-action: challenge
```

Same with a desktop Chrome User-Agent (2026-10-09 07:38 UTC). A file published for AI agents is behind a JavaScript
challenge that agents and `curl` cannot pass; our agent only got it through a browser-backed fetcher.

**What the AI agent got wrong from the docs:** it assumed `type=1` is the only provider (the tokenized-securities skill
says so, see §2.1) and initially resolved bare tickers to Ondo only; it also assumed `stockInfo.price` exists for every
issuer.

## 2. Documentation issues

One per line: page, section, what is wrong, what it should say.

1. `skills/binance-web3/binance-tokenized-securities-info/SKILL.md` (v1.1), *API 1 → Request Parameters → type*: says
   "`1` = Ondo Finance (currently the only supported tokenized stock provider)". Live answers on 2026-10-09:
   `type=1` 1,366 rows, `type=2` (xStocks) 269, `type=3` (bStocks) 91, `type=4` 4 (`xKLSH`, `xOPAI`, `xSPCX`, `pPOLY` —
   pre-IPO names), `type=9` 1 (`BNC4`). Should list every type and what it means; types 4 and 9 are documented nowhere.
   (The agentic-wallet SKILL.md does document 1/2/3 and even warns that "older versions" of this skill only know type 1.)
2. Same page, *Supported Chains*: lists `1` and `56`. The `type=1` and `type=2` answers also contain `chainId: "CT_501"` (Solana).
3. Same page, *API 5 → stockInfo.price*: "May be `null` outside trading hours". For bStocks it was `null` in every one of
   our samples, including weekday overnight hours when Ondo's `stockInfo.price` was live; bStocks also return `null`
   `totalHolders`, `circulatingSupply`, `marketCap`. `statusInfo.marketStatus`, `nextOpenTime`, `nextCloseTime` are `null`
   for xStocks and bStocks. Should state field availability per provider.
4. Same page, *API 3 Market Status*: the live answer has an undocumented `offhours` object
   (`{"openState":false,"nextOpenTime":1791590760000,"nextCloseTime":1791762900000}` = Sat 00:06 → Sun 23:55 UTC) — the
   weekend trading window. It is the single most useful field for weekend logic and is not in the docs.
5. Same page, *API 6 Token K-Line*: index 5 is "Reserved" and always `"0"`, so there is no on-chain volume per candle, and
   hours without trades are silently missing. Should say "missing hour = no trade" and return volume.
6. Same page, *API 5 tokenInfo.price*: no timestamp. On thin pools the value can be days old; the only hint is
   `priceChange24h == "0"`. Should document this and add a last-trade time.
7. `binance-agentic-wallet/SKILL.md` command table lists `wallet gas-price` with no parameters; `baw` 1.10.0 answers
   `error: required option '--binanceChainId <binanceChainId>' not specified`.
8. Hackathon page says the demo video is "strongly recommended but optional"; the Submit Project form marks
   *Demo video URL* as required.
9. Authentication page: should name the portal's login methods and the IP geo-check before the developer reaches the dialog.

**Missing topics:** how the public `bapi` "…/ai" endpoints relate to the signed Web3 API (same data? same SLA?); per-issuer
differences (Ondo vs xStocks vs bStocks) in one table; where the reference/oracle price comes from for each issuer;
weekend and holiday behaviour of each issuer; whether xStocks/bStocks prices are DEX prints or oracle values.

**Code examples:** all the `curl` examples in the tokenized-securities skill ran as written. We did not run the
signed-API examples (no key).

**Most useful page:** https://github.com/binance/binance-skills-hub/blob/main/skills/binance-web3/binance-tokenized-securities-info/SKILL.md

## 3. API pitfalls

**Data we could not trust.**

- *Token K-Line, xStocks on BSC.* Hourly candles with impossible prices, not flagged:
  `AAPLx` (`0x9d275685dc284c8eb1c79f6aba7a63dc75ec890a`) highs of 68,593 → 1,600,182 → 36,477,356 USD on 2026-05-12/13;
  `TSLAx` 689,700 on 2026-05-09; `SPYx` 13,311 on 2026-05-25; `QQQx` 12,828 then 84.01; `METAx` 10,978 then 69.73.
  Reproduce: `GET …/dex/market/token/kline/ai?chainId=56&contractAddress=0x9d27…890a&interval=1h&limit=300` and page back
  with `endTime`.
- *RWA Dynamic V2, xStocks on BSC.* Prices far from the stock and days old, with no timestamp (2026-10-09 07:08 UTC):
  `METAx` 572.44/share vs `METAon` 722.35 and `stockInfo.price` 722.41 (−20.8 %); `MSTRx` 138.86 vs 154.67 (−10.2 %);
  `COINx` 198.89 vs 174.89 (+13.7 %). Each had `priceChange24h: "0"`. 7 of the 10 xStocks tokens we poll had not traded for
  24 h. A wallet UI or agent reading `tokenInfo.price` would mark positions 10–20 % wrong.
- *`tokenInfo.volume24h`* is identical across issuers (`TSLAx` and `TSLAon` both `20588529710.527589126456` on 2026-10-05):
  it is the stock's volume, as the skill warns, but it is the only "volume" field and invites misuse.
- *Supply reconciliation (good news).* `TSLAon` `circulatingSupply` `24762.208422651843923785` equals on-chain
  `totalSupply()` exactly; `TSLAx` 20,000 = 20,000. `TSLAB` returns `circulatingSupply: null` while on-chain
  `totalSupply()` is 61,802.8053. Decimals are 18 on-chain for all three.

**Edge cases.**

- *K-Line `endTime`*: `endTime = <open time>` returns that candle, `<open time> − 1` excludes it. Consistent with the docs,
  but "based on candle open time minus 1 ms" reads as if the API subtracts it.
- *bStocks history* starts 2026-09-13/14 for every ticker we pulled (exactly 599 hourly candles each on 2026-10-09);
  COINB has gaps (520). Not an error, but nothing tells you when an issuer launched on a chain.
- *Market status at session switches*: at 07:55–08:01 UTC the answer is `openState: true`, `marketStatus: "overnight"`,
  `nextClose` 07:55 and `nextOpen` 08:01 — the six-minute pause between overnight and pre-market. Correct per the docs'
  interpretation note, but easy to misread.

**Unclear error messages.** `baw market-order quote …` without a session returns
`{"code":10003000,"name":"NOT_LOGGED_IN","message":"Not logged in"}` — clear, but it applies to a read-only quote and to
`baw wallet chains` (static list), which a developer does not expect to need a login. Nothing in the message says that
signing in requires a Binance account and the Binance app.

**Latency** (2026-10-09 ~07:40 UTC, EU network, 5–7 calls each, after TLS warm-up): token list 95 ms median; market status
60; asset status 60; RWA Dynamic V2 62; K-Line 300 × 1h 65; BSC `eth_blockNumber` 74. First call ~300 ms. No complaints.

**Rate limits:** never hit. We paced ourselves at 2 requests/s (history download: 276 requests; weekend poller: 31
requests every 10 minutes).

**Authentication and signing:** we implemented the documented scheme (`timestamp + METHOD + requestPath(with /build) +
body`, HMAC-SHA256, Base64, ISO-8601 ms timestamp) and unit-tested it against the formula, but could not test it live.

## 4. AI stack feedback

**Used:** Wallet Skills (the tokenized-securities skill's endpoints, live) and the Wallet Skills CLI (`baw` 1.10.0,
installed with `npm install @binance/agentic-wallet@1.10.0`, read-only commands). **Not used:** Agentic Wallet sign-in,
BNB Agent Studio.

**What worked:** SKILL.md files are the best docs in the ecosystem for an agent — exact URLs, field tables, gotchas.
`baw --help` and per-command `--help` are precise; every command takes `--json` and errors come back as JSON with a
numeric code and a name. `baw cli-check`, `baw skill-check` and `baw wallet status` work without a session. We validated
the commands our guard emits against the real parser: `baw market-order quote --fromTokenQty 875 --fromToken 0x55d3…7955
--toToken 0x5b19…292f --binanceChainId 56 --slippage 1 --json` passes option parsing and stops at `NOT_LOGGED_IN`
(this check is an opt-in test in our repo).

**What did not work for us:** everything useful needs a session — even `market-order quote` and `wallet chains`. A
session needs a Binance account, an MPC wallet created in the Binance app, and QR sign-in. There is no sandbox, no
testnet (`wallet chains` documents 56, 1, 8453, CT_501), no quote-only mode. A builder without a Binance account — for
example, a resident of a country that is *not* on the hackathon's restricted list but is not served by Binance accounts —
can read about the AI execution layer but cannot run any of it.

**Missing:**
- a **pre-trade policy hook** in the Agentic Wallet: let a third-party guard (like ours) veto or resize an order before the
  user is asked to confirm. Today the only guardrails are the app-side daily limit / token scope / high-risk confirmation;
  none of them knows that the stock market is closed or that a pool is 20 % off the stock;
- session-aware guardrails for RWA: refuse or warn on stock tokens while the venue reports `ASSET_PAUSED`, or when the
  quote is stale;
- `market-order quote` without login (public quotes, as the skill's own docs present it — "get a quote without trading");
- a testnet / paper mode for the wallet CLI.

**BNB Agent Studio:** not used.

## 5. Tokenized-stock specifics

**Platforms we worked with:** Ondo, xStocks, bStocks (data only — we did not execute trades).

**Liquidity:** we did not trade, so we have no depth numbers. What the data shows: Ondo and bStocks tokens print every
hour, including weekends (Ondo since June 2026 on BSC; bStocks since their launch in mid-September); xStocks on BSC printed
5–255 hourly candles per ticker in five months. Ondo exposes `limitInfo.maxActiveNotionalValue` (TSLAon 975,000 USD,
SPYon 4,500,000 USD); xStocks and bStocks return `limitInfo: null`.

**Slippage:** not measured (a quote needs a signed-in Agentic Wallet).

**Outside market hours:** Ondo runs `overnight` on weekdays (six-minute pause 07:55–08:01 UTC) and an `offhours` weekend
window (on 10–11 Oct 2026: Sat 00:06 → Sun 23:55 UTC). Weekend on-chain moves were small (TSLA within ±0.6 % of Friday's close on 3–4 Oct) while
Friday→Monday gaps reached +8.9 % (MSTR, 18–21 Sep) and +11.8 % (CRCL). Over 9–16 weekends per ticker with weekend
trading, the Sunday-20:00-ET price had shown under 10 % of Monday's move for most single stocks (TSLA +4 %, NVDA −14 %,
AAPL −4 %, COIN −1 %; AMZN +21 %) and about a quarter for SPY/QQQ — the weekend price is not a good mark.

**On-chain vs underlying:** Ondo tracks `stockInfo.price` within ~0.1 % whenever the stock prints (TSLAon 378.76/share vs
378.70, 2026-10-09 07:08 UTC); bStocks sit within ~0.15 % of Ondo; xStocks on BSC drift up to 20 % away and stay there
because nobody trades them. This was actionable: our guard blocks any token more than 3 % from the cross-issuer median.

**Differences between representations:** multipliers differ per issuer for the same stock (SPYon 1.00947, SPYx 1.00571,
NVDAB 1.00078) so prices must be compared per share; only Ondo carries `stockInfo.price`, session status and limits;
bStocks lack holders/supply in the API; xStocks need staleness handling. We special-cased all three.

## 6. Redesign suggestions

**First five minutes we would want:** land on the portal, pick "public data" → get a read-only key by email or GitHub
in one click (or no key at all for read endpoints), see one copy-paste `curl` that returns TSLA on BSC for all three
issuers side by side, then a second one for the Transaction API dry-run. Show the region check *before* the login dialog.
Serve `llms.txt` and the `.md` docs without the WAF challenge so agents can read them.

**One change that would have saved us the most time:** a way to get a Developer Portal key (or run `baw market-order quote`)
without a Binance account.

## 7. Requested capabilities

- `lastTradeTime` (or quote timestamp) in RWA Dynamic V2 — to tell a live price from a stale one.
- Per-issuer liquidity: max notional within 0.5 % / 1 % price impact, for buy and sell.
- Official reference close per ticker (last regular-session close with its timestamp) — the anchor for weekend logic.
- Historical asset status (pauses, corporate actions, earnings windows) — to backtest guards honestly.
- Outlier flags on K-Line and on-chain volume per candle.
- WebSocket/webhook for `marketStatus` / `reasonCode` changes.
- Agentic Wallet pre-trade policy hook, public quotes, and a testnet/paper mode.
- Documented `offhours` window and a per-issuer trading-hours table.

**Will we keep building on it?** Probably — the read side is fast and clean; the execution side needs to open up first.
