"""On-disk formats for recorded market data — so every demo and test is reproducible offline.

history/<SYMBOL>_1h.json   hourly candles of one token, pulled from the public Token K-Line endpoint
live/<YYYY-MM-DD>.jsonl    one line per polling cycle: market status + RWA Dynamic V2 for each token,
                           stored as the raw `data` object the API returned (nothing re-computed)
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .models import Candle, TokenRef

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
HISTORY_DIR = DATA_DIR / "history"
LIVE_DIR = DATA_DIR / "live"


@dataclass
class TokenHistory:
    token: TokenRef
    interval: str
    candles: list[Candle]
    fetched_ms: int
    source: str

    def to_json(self) -> dict[str, Any]:
        t = self.token
        return {
            "symbol": t.symbol, "ticker": t.ticker, "provider": t.provider, "type": t.type_id,
            "chain_id": t.chain_id, "address": t.address, "decimals": t.decimals, "multiplier": t.multiplier,
            "interval": self.interval, "fetched_ms": self.fetched_ms, "source": self.source,
            "candles": [[c.open_ms, c.open, c.high, c.low, c.close, c.close_ms] for c in self.candles],
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "TokenHistory":
        token = TokenRef(chain_id=str(d["chain_id"]), address=d["address"], symbol=d["symbol"], ticker=d["ticker"],
                         type_id=int(d["type"]), multiplier=float(d.get("multiplier") or 1.0),
                         decimals=int(d.get("decimals") or 18))
        candles = [Candle(open_ms=int(r[0]), open=float(r[1]), high=float(r[2]), low=float(r[3]),
                          close=float(r[4]), close_ms=int(r[5])) for r in d["candles"]]
        return cls(token, d["interval"], candles, int(d["fetched_ms"]), d.get("source", ""))


def history_path(symbol: str, interval: str = "1h", base: Path = HISTORY_DIR) -> Path:
    return base / f"{symbol}_{interval}.json"


def save_history(h: TokenHistory, base: Path = HISTORY_DIR) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    path = history_path(h.token.symbol, h.interval, base)
    path.write_text(json.dumps(h.to_json(), separators=(",", ":")), encoding="utf-8")
    return path


def load_history(symbol: str, interval: str = "1h", base: Path = HISTORY_DIR) -> TokenHistory:
    return TokenHistory.from_json(json.loads(history_path(symbol, interval, base).read_text(encoding="utf-8")))


def available_history(base: Path = HISTORY_DIR, interval: str = "1h") -> list[str]:
    return sorted(p.name[: -len(f"_{interval}.json")] for p in base.glob(f"*_{interval}.json"))


def append_live(record: dict[str, Any], day: str, base: Path = LIVE_DIR) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{day}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")
    return path


def iter_live(base: Path = LIVE_DIR) -> Iterator[dict[str, Any]]:
    for path in sorted(base.glob("*.jsonl")):
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue          # a half-written last line after a crash is skipped, not fatal
