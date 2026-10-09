# Demo video — script

`docs/demo.mp4` · about 2 minutes · 1920×1080, 30 fps, H.264 · no voice · English captions burned in and in
`docs/demo.en.srt`. Rendered by `scripts/record_demo.py` from the real dashboard (`docs/`) and the real CLI: terminal
scenes show output captured while recording (live public endpoints, the real `baw` 1.10.0 CLI, the real test run).
The script refuses to render if the live check, the `baw` syntax check or the tests fail.

| # | Time | Scene | Caption |
|---|---|---|---|
| 1 | 0:00 | Title card | Weekend Gap Guard: a pre-trade risk gate for tokenized stocks on BNB Chain. |
| 2 | 0:04 | Dashboard hero | From Friday 16:00 to Monday 09:30 New York time the stock market prints nothing. Ondo, xStocks and bStocks tokens keep trading on BSC. |
| 3 | 0:12 | Hero, verdict pills | A stop-loss cannot fill at a price the market never prints. The guard answers every order with ALLOW, REDUCE or BLOCK, and says why. |
| 4 | 0:20 | KPI tiles | All numbers come from real BSC data pulled from the public Binance Wallet endpoints, with no API key. |
| 5 | 0:27 | TSLA weekend chart, cursor sweeps the weekend | A real weekend, hour by hour: TSLA from all three issuers. The dashed line is Friday's close, the frozen reference; red and amber strips are the guard's verdicts. |
| 6 | 0:36 | Key moment: Friday open → ALLOW | Friday, market open: the stop can fill, so a 2,000 dollar buy is allowed. |
| 7 | 0:41 | Last hour → REDUCE 1,250 | Last hour before the close: you are about to carry weekend risk. The cap is halved. |
| 8 | 0:46 | Saturday noon → REDUCE 875, risk check highlighted | Saturday: risk is priced on the weekend gap, 9.6% here, not on the 3% stop. 2,000 becomes 875. |
| 9 | 0:51 | Switch to TSLAx → BLOCK | The xStocks pool for TSLA on BSC had not traded for hours: no live mark, so the guard blocks it. |
| 10 | 0:57 | MSTR weekend chart | MSTR, the largest Monday gap in our record: +8.9%. The token climbs into Monday's pre-open... |
| 11 | 1:03 | Monday pre-open → REDUCE 625, drift check | ...and once the weekend drift passes one sigma of a weekend move, the cap is halved again: 625 dollars. |
| 12 | 1:10 | Live board (30 tokens) | The live board, recorded from the public endpoints: Ondo and bStocks agree within 0.15%. Most xStocks pools on BSC are stale or up to 20% off the stock, and are blocked. |
| 13 | 1:21 | Calibration table | Out of sample, no Monday gap in 540 ticker-weekends exceeded the guard's 99% estimate. |
| 14 | 1:26 | Stress panel | Saturday buys of 5,000 dollars over 129 real weekends: worst Monday loss minus 218 dollars raw, minus 38 guarded. |
| 15 | 1:33 | Terminal: `gapguard check TSLAB --usd 2000 --wallet-plan` (live) | The same engine runs as a CLI and as an Agent Skill. This is a live call to the public endpoints. |
| 16 | 1:38 | Same, wallet hand-off lines | It hands the allowed size to the Binance Agentic Wallet as a quote, with a slippage cap. |
| 17 | 1:43 | Terminal: the generated `baw market-order quote …` → `NOT_LOGGED_IN` | Checked against the real baw 1.10.0 CLI: the command parses and stops at sign-in. Signing needs a Binance account, so the guard never executes. |
| 18 | 1:51 | Terminal: `python -m pytest -q` | Everything is tested offline. |
| 19 | 1:54 | Closing card with repo URL | Weekend Gap Guard: size tokenized-stock trades for the market that is actually open. |

Re-render:

```bash
pip install -e ".[video]"            # playwright + imageio-ffmpeg; Chromium via `python -m playwright install chromium`
gapguard site                        # refresh docs/data first
python scripts/record_demo.py --baw path/to/node_modules/@binance/agentic-wallet/dist/index.js
```
