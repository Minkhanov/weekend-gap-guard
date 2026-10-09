"""Weekend replay, out-of-sample calibration, stress, and the CLI — on a synthetic book (no network)."""
import json
from datetime import timezone

import pytest
from test_gap_and_views import START, hourly, stepped_price

from gapguard import cli, scenarios
from gapguard.models import TokenRef
from gapguard.views import HistoryBook, Series

UTC = timezone.utc


@pytest.fixture
def book():
    ondo = TokenRef("56", "0x" + "a1" * 20, "XYZon", "XYZ", 1, multiplier=1.0)
    bst = TokenRef("56", "0x" + "b2" * 20, "XYZB", "XYZ", 3, multiplier=1.0)
    xst = TokenRef("56", "0x" + "c3" * 20, "XYZx", "XYZ", 2, multiplier=1.0)
    candles = hourly(START, 24 * 7 * 8, stepped_price)
    sparse = candles[::40]                               # an illiquid deployment: one trade every 40 h
    return HistoryBook({"XYZon": Series(ondo, candles), "XYZB": Series(bst, candles), "XYZx": Series(xst, sparse)})


def test_weekend_timeline_shape_and_verdicts(book):
    c = scenarios.find_weekend(book, "XYZ", "2026-09-11")
    tl = scenarios.weekend_timeline(book, "XYZ", c, order_usd=2000)
    assert tl["symbols"] == ["XYZon", "XYZx", "XYZB"]
    phases = {p["phase"] for p in tl["points"]}
    assert {"open", "pre_close", "closed", "pre_open"} <= phases
    sat = next(p for p in tl["points"] if p["phase"] == "closed")
    assert sat["tokens"]["XYZon"]["verdict"] == "REDUCE"
    assert tl["outcome"]["gap"] == pytest.approx(0.02, rel=1e-3)
    assert tl["outcome"]["exceeded_q99"] in (True, False)
    assert set(tl["snapshots"]) == {"fri_open", "fri_pre_close", "sat_noon", "sun_evening", "mon_pre_open", "mon_after_open"}
    json.dumps(tl, default=str)


def test_sparse_issuer_is_blocked_for_stale_quotes(book):
    c = scenarios.find_weekend(book, "XYZ", "2026-09-11")
    tl = scenarios.weekend_timeline(book, "XYZ", c, order_usd=2000)
    verdicts = [p["tokens"]["XYZx"]["verdict"] for p in tl["points"]]
    assert verdicts.count("BLOCK") > len(verdicts) // 2


def test_calibration_is_out_of_sample(book):
    cal = scenarios.calibration(book, "XYZ")
    assert cal["weekends"] >= 3
    for r in cal["rows"]:
        assert r["q99"] > 0
    assert cal["exceed_q99"] == sum(r["exceeded"] for r in cal["rows"])


def test_stress_guarded_never_bigger_than_raw(book):
    s = scenarios.stress(book, ["XYZ"], order_usd=5000)
    assert s["n"] >= 3
    for t in s["trades"]:
        assert t["guarded_usd"] <= 5000
        assert abs(t["guarded_pnl"]) <= abs(t["raw_pnl"]) + 1e-9


def test_find_weekend_unknown_date(book):
    with pytest.raises(ValueError):
        scenarios.find_weekend(book, "XYZ", "2020-01-03")


def test_cli_check_replay_json(book, monkeypatch, capsys):
    monkeypatch.setattr(HistoryBook, "load", classmethod(lambda cls, symbols=None: book))
    rc = cli.main(["check", "XYZB", "--usd", "2000", "--at", "2026-09-12T16:00Z", "--json", "--wallet-plan"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    # synthetic Saturday: +1 % drift = 1.3 σ of this calm series → weekend cap 875 halved
    assert out["verdict"] == "REDUCE" and out["allowed_usd"] == 437.5
    assert next(c for c in out["checks"] if c["name"] == "reference.drift")["status"] == "cut"
    assert out["agentic_wallet"]["execute"] is False
    assert out["metrics"]["source"] == "replay:history-1h"


def test_cli_check_exit_code_on_block(book, monkeypatch, capsys):
    monkeypatch.setattr(HistoryBook, "load", classmethod(lambda cls, symbols=None: book))
    rc = cli.main(["check", "XYZx", "--usd", "2000", "--at", "2026-09-12T18:00Z", "--exit-code"])
    out = capsys.readouterr().out
    assert rc == 3 and out.startswith("[BLOCK]")


def test_cli_status_offline(capsys):
    assert cli.main(["status", "--at", "2026-10-10T16:00Z"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["phase"] == "closed" and info["closure"]["kind"] == "weekend"


def test_cli_scenario_writes_json(book, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(HistoryBook, "load", classmethod(lambda cls, symbols=None: book))
    out = tmp_path / "tl.json"
    assert cli.main(["scenario", "XYZ", "--friday", "2026-09-11", "--json", str(out)]) == 0
    assert json.loads(out.read_text())["ticker"] == "XYZ"
