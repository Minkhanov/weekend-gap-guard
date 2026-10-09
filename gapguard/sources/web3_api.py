"""Optional signed client for the Binance Web3 API (https://web3.binance.com/build).

Used only when BINANCE_WEB3_API_KEY / BINANCE_WEB3_SECRET_KEY are set (developer-portal project).
Signing follows the published Authentication page:

    preHash = timestamp + METHOD + requestPath + body
    X-OC-SIGN = base64(HMAC_SHA256(secret, preHash))

where `timestamp` is ISO-8601 UTC with milliseconds (2026-05-11T10:08:57.715Z), `requestPath` is
the full path *including the /build prefix* plus the query string, and `body` is "" for GET.

Status: the signer is unit-tested against the documented algorithm; live calls have NOT been
verified yet (no developer-portal key during the build) — see docs/DX-NOTES.md.
The client is read-only by construction: it exposes quote/simulate (dry-run) and data reads,
and has no method that broadcasts a transaction.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from .http import PoliteClient, SourceError

HOST = "https://web3.binance.com"
PREFIX = "/build"

# Paths as listed in the Market / Wallet / Transaction / Trading API overviews.
PATHS = {
    "rwa_platforms": "/api/v1/dex/market/rwa/platforms",
    "rwa_price": "/api/v1/dex/market/rwa/price",
    "rwa_tokens": "/api/v1/dex/market/rwa/tokens",
    "rwa_underlying_market": "/api/v1/dex/market/rwa/underlying-market",
    "candles": "/api/v1/dex/market/candles",
    "balances": "/api/v1/dex/balance/all-token-balances-by-address",
    "quote": "/api/v1/dex/aggregator/quote",
    "simulate": "/api/v1/dex/pre-transaction/simulate",
}


def iso_ms(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def sign(secret: str, timestamp: str, method: str, request_path: str, body: str = "") -> str:
    pre_hash = f"{timestamp}{method.upper()}{request_path}{body}"
    digest = hmac.new(secret.encode(), pre_hash.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


class Web3ApiClient:
    def __init__(self, api_key: str, secret: str, http: PoliteClient | None = None, *, recv_window_ms: int = 5000):
        if not api_key or not secret:
            raise ValueError("Binance Web3 API key and secret are required")
        self.api_key = api_key
        self._secret = secret
        self.recv_window_ms = recv_window_ms
        self.http = http or PoliteClient(min_interval=0.25)

    def signed_headers(self, method: str, request_path: str, body: str = "", *, now: datetime | None = None) -> dict[str, str]:
        ts = iso_ms(now or datetime.now(timezone.utc))
        return {
            "X-OC-APIKEY": self.api_key,
            "X-OC-TIMESTAMP": ts,
            "X-OC-SIGN": sign(self._secret, ts, method, request_path, body),
            "X-OC-RECV-WINDOW": str(self.recv_window_ms),
            "Content-Type": "application/json",
        }

    def call(self, name: str, *, params: dict[str, Any] | None = None, payload: Any = None) -> Any:
        path = PREFIX + PATHS[name]
        query = ("?" + urlencode(params)) if params else ""
        method = "POST" if payload is not None else "GET"
        body = json.dumps(payload, separators=(",", ":")) if payload is not None else ""
        headers = self.signed_headers(method, path + query, body)
        resp = self.http.request(method, HOST + path + query, headers=headers, content=body or None)
        data = resp.json()
        if not isinstance(data, dict) or data.get("code") not in (0, "0"):
            raise SourceError(f"web3 api {name}: code {data.get('code') if isinstance(data, dict) else '?'}: "
                              f"{(data.get('msg') or data.get('message')) if isinstance(data, dict) else data}")
        return data.get("data")

    # Convenience wrappers — parameter names follow the docs where they were visible; [unverified].
    def rwa_price(self, **params: Any) -> Any:
        return self.call("rwa_price", params=params)

    def simulate(self, tx: dict[str, Any]) -> Any:
        """Dry-run only: the Transaction API simulates, nothing is signed or broadcast here."""
        return self.call("simulate", payload=tx)
