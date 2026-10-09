"""Sources (offline: replay dir + mocked transport), signing, RPC encoding and the Agentic Wallet hand-off."""
import base64
import hashlib
import hmac
import json
import shlex
from datetime import datetime, timezone

import httpx
import pytest
from conftest import FIXTURES, FRI_OPEN, SAT_NOON, make_view

from gapguard.integrations.agentic_wallet import PAY_TOKENS, plan
from gapguard.models import TokenRef
from gapguard.risk.account import demo_account
from gapguard.risk.engine import OrderIntent, evaluate
from gapguard.sources.binance_rwa import RwaClient, cache_key
from gapguard.sources.bsc_rpc import balance_of_calldata, decode_uint, is_address
from gapguard.sources.http import PoliteClient, SourceError
from gapguard.sources.web3_api import Web3ApiClient, iso_ms, sign

TSLAB = TokenRef("56", "0x5b1910eaad6450e50f816082aa078c41f10c292f", "TSLAB", "TSLA", 3)


def test_rwa_client_replays_recorded_answers(tmp_path):
    t = TokenRef("56", "0x2494b603319d4d9f9715c9f4496d9e0364b59d93", "TSLAon", "TSLA", 1)
    params = {"chainId": "56", "contractAddress": t.address}
    (tmp_path / cache_key("dynamic", params)).write_text((FIXTURES / "dynamic_TSLAon.json").read_text())
    (tmp_path / cache_key("asset_status", params)).write_text((FIXTURES / "asset_status_TSLAon.json").read_text())
    c = RwaClient(replay_dir=tmp_path)
    snap = c.snapshot(t)
    assert snap.price == pytest.approx(371.688571) and snap.holders == 29898
    assert c.asset_status(t).reason_code == "TRADING"
    with pytest.raises(SourceError):
        c.market_status()                         # not in the snapshot dir → explicit error, no network


def test_rwa_client_rejects_api_error_envelope():
    def handler(request):
        return httpx.Response(200, json={"code": "000002", "message": "illegal parameter", "success": False})
    http = PoliteClient(transport=httpx.MockTransport(handler), min_interval=0, sleep=lambda s: None)
    with pytest.raises(SourceError, match="000002"):
        RwaClient(http).market_status()


def test_polite_client_retries_429_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={"ok": True})
    slept = []
    http = PoliteClient(transport=httpx.MockTransport(handler), min_interval=0, sleep=slept.append)
    assert http.get_json("https://example.test/x") == {"ok": True}
    assert calls["n"] == 2 and 1.0 in slept


def test_polite_client_empty_202_is_an_error():
    http = PoliteClient(transport=httpx.MockTransport(lambda r: httpx.Response(202, content=b"")),
                        min_interval=0, sleep=lambda s: None)
    with pytest.raises(SourceError):
        http.get_json("https://example.test/waf")


def test_klines_parse_and_sort():
    body = json.loads((FIXTURES / "kline1h_TSLAB.json").read_text())
    rows = body["data"]["klineInfos"]
    shuffled = {"code": "000000", "success": True, "data": {"klineInfos": list(reversed(rows))}}
    http = PoliteClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=shuffled)),
                        min_interval=0, sleep=lambda s: None)
    candles = RwaClient(http).klines(TSLAB, "1h", 300)
    assert len(candles) == len(rows)
    assert all(a.open_ms < b.open_ms for a, b in zip(candles, candles[1:]))


def test_web3_api_signature_matches_documented_algorithm():
    ts = iso_ms(datetime(2026, 5, 11, 10, 8, 57, 715000, tzinfo=timezone.utc))
    assert ts == "2026-05-11T10:08:57.715Z"
    path = "/build/api/v1/dex/market/rwa/price?chainId=56"
    expected = base64.b64encode(hmac.new(b"secret", f"{ts}GET{path}".encode(), hashlib.sha256).digest()).decode()
    assert sign("secret", ts, "get", path) == expected
    h = Web3ApiClient("key", "secret").signed_headers("GET", path, now=datetime(2026, 5, 11, 10, 8, 57, 715000, tzinfo=timezone.utc))
    assert h["X-OC-SIGN"] == expected and h["X-OC-APIKEY"] == "key" and h["X-OC-TIMESTAMP"] == ts


def test_rpc_encoding():
    owner = "0x" + "ab" * 20
    data = balance_of_calldata(owner)
    assert data.startswith("0x70a08231") and len(data) == 10 + 64 and data.endswith("ab" * 20)
    assert decode_uint("0x0de0b6b3a7640000") == 10 ** 18 and decode_uint("0x") == 0
    assert is_address(owner) and not is_address("0x123")
    with pytest.raises(ValueError):
        balance_of_calldata("not-an-address")


# ------------------------------------------------------------------------------------- wallet hand-off
def test_wallet_plan_buy_uses_allowed_size_and_never_executes():
    d = evaluate(OrderIntent("TSLAB", "buy", 2000), make_view(SAT_NOON), demo_account())
    w = plan(d, TSLAB.address, 380.0)
    assert w.execute is False and w.verdict == "REDUCE"
    args = shlex.split(w.swap_cmd)
    assert args[:3] == ["baw", "market-order", "swap"] and args[-1] == "--json"
    kv = dict(zip(args[3:-1:2], args[4:-1:2]))
    assert kv["--fromTokenQty"] == "875" and kv["--fromToken"] == PAY_TOKENS["USDT"]
    assert kv["--toToken"] == TSLAB.address and kv["--binanceChainId"] == "56" and kv["--slippage"] == "1"
    assert shlex.split(w.quote_cmd)[2] == "quote"


def test_wallet_plan_sell_converts_usd_to_token_quantity():
    d = evaluate(OrderIntent("TSLAB", "sell", 1000), make_view(FRI_OPEN), demo_account())
    w = plan(d, TSLAB.address, 400.0, pay="USDC")
    kv = dict(zip(shlex.split(w.swap_cmd)[3:-1:2], shlex.split(w.swap_cmd)[4:-1:2]))
    assert kv["--fromTokenQty"] == "2.5" and kv["--toToken"] == PAY_TOKENS["USDC"] and kv["--slippage"] == "0.5"


def test_wallet_plan_block_has_no_command():
    d = evaluate(OrderIntent("TSLAB", "buy", 2000), make_view(SAT_NOON, price=None), demo_account())
    w = plan(d, TSLAB.address, None)
    assert w.verdict == "BLOCK" and w.swap_cmd is None and w.quote_cmd is None and w.execute is False
    with pytest.raises(ValueError):
        plan(d, TSLAB.address, None, pay="DOGE")


@pytest.mark.skipif(not __import__("os").environ.get("GAPGUARD_BAW_JS"),
                    reason="set GAPGUARD_BAW_JS to @binance/agentic-wallet/dist/index.js to run against the real CLI")
def test_generated_quote_passes_the_real_baw_option_parser():
    """The real CLI must reject nothing in our command: without a session it has to stop at NOT_LOGGED_IN."""
    import os
    import subprocess
    d = evaluate(OrderIntent("TSLAB", "buy", 2000), make_view(SAT_NOON), demo_account())
    args = shlex.split(plan(d, TSLAB.address, 380.0).quote_cmd)[1:]
    out = subprocess.run(["node", os.environ["GAPGUARD_BAW_JS"], *args], capture_output=True, text=True, timeout=60)
    body = json.loads(out.stdout)
    assert body["success"] is False and body["error"]["name"] == "NOT_LOGGED_IN"
