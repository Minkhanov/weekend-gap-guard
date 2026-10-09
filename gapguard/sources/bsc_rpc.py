"""Read-only BSC JSON-RPC: ERC-20 `balanceOf` for a watch-only address across stock tokens.

No keys, no signing, no transactions — `eth_call` only, batched (JSON-RPC batch) and paced.
Default endpoint: the public BNB Chain dataseed.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

from ..models import TokenRef
from .http import PoliteClient, SourceError

log = logging.getLogger("gapguard.rpc")

DEFAULT_RPC = "https://bsc-dataseed.bnbchain.org"
BALANCE_OF = "0x70a08231"          # keccak256("balanceOf(address)")[:4]
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def is_address(value: str) -> bool:
    return bool(ADDRESS_RE.match(value or ""))


def balance_of_calldata(owner: str) -> str:
    if not is_address(owner):
        raise ValueError(f"not an EVM address: {owner!r}")
    return BALANCE_OF + owner[2:].lower().rjust(64, "0")


def decode_uint(hex_value: str | None) -> int:
    if not hex_value or hex_value == "0x":
        return 0
    return int(hex_value, 16)


class BscRpc:
    def __init__(self, url: str = DEFAULT_RPC, http: PoliteClient | None = None, *, batch_size: int = 40):
        self.url = url
        self.http = http or PoliteClient(min_interval=0.5)
        self.batch_size = batch_size

    def block_number(self) -> int:
        body = self.http.post_json(self.url, {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []})
        if "error" in body:
            raise SourceError(f"eth_blockNumber: {body['error']}")
        return decode_uint(body.get("result"))

    def balances(self, owner: str, tokens: Iterable[TokenRef]) -> dict[str, float]:
        """Return {symbol: amount} for every token with a non-zero balance (amount in token units)."""
        data = balance_of_calldata(owner)
        tokens = list(tokens)
        out: dict[str, float] = {}
        for start in range(0, len(tokens), self.batch_size):
            chunk = tokens[start:start + self.batch_size]
            batch = [{"jsonrpc": "2.0", "id": i, "method": "eth_call",
                      "params": [{"to": t.address, "data": data}, "latest"]} for i, t in enumerate(chunk)]
            answer = self.http.post_json(self.url, batch)
            if not isinstance(answer, list):
                raise SourceError(f"RPC batch not supported or failed: {str(answer)[:120]}")
            by_id = {item.get("id"): item for item in answer if isinstance(item, dict)}
            for i, t in enumerate(chunk):
                item = by_id.get(i) or {}
                if "error" in item:
                    log.debug("balanceOf %s failed: %s", t.symbol, item["error"])
                    continue
                raw = decode_uint(item.get("result"))
                if raw:
                    out[t.symbol] = raw / (10 ** t.decimals)
        return out
