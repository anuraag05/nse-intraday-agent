import os
import sys

import numpy as np
import pandas as pd
import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from trader.agent import review_candidates
from trader.backtest import backtest
from trader.broker import PaperBroker
from trader.data import synthetic
from trader.risk import can_open, position_size
from trader.strategy import add_indicators, latest_signals

CFG = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "..", "config.yaml")))


def test_position_size_risk_and_cap():
    r = CFG["risk"]
    # risk 1% of 500k = 5000; risk/share 10 -> 500 shares by risk; cap 20% = 100k/100 = 1000 shares
    assert position_size(500000, 100, 90, r) == 500
    # tight stop -> cap binds: 100k / 100 = 1000
    assert position_size(500000, 100, 99.9, r) == 1000
    assert position_size(500000, 100, 101, r) == 0


def test_can_open_limits():
    r = CFG["risk"]
    assert can_open(5, 500000, 500000, r)[0] is False
    assert can_open(1, 480000, 500000, r)[0] is False  # -4% day
    assert can_open(1, 499000, 500000, r)[0] is True


def test_signal_no_lookahead():
    df = synthetic("TEST", 300, seed=1)
    full = add_indicators(df, CFG["strategy"])
    part = add_indicators(df.iloc[:200], CFG["strategy"])
    # indicators/signals on day 199 must not change when future data is appended
    assert bool(full["signal"].iloc[199]) == bool(part["signal"].iloc[-1])
    assert np.isclose(full["atr"].iloc[199], part["atr"].iloc[-1])


def test_forced_breakout_signal():
    idx = pd.bdate_range("2024-01-01", periods=120)
    close = np.linspace(100, 130, 120)
    df = pd.DataFrame({"open": close, "high": close * 1.005, "low": close * 0.995, "close": close,
                       "volume": 1e6}, index=idx)
    df.iloc[-1, df.columns.get_loc("close")] = 140
    df.iloc[-1, df.columns.get_loc("high")] = 141
    df.iloc[-1, df.columns.get_loc("volume")] = 4e6
    sigs = latest_signals({"X": df}, CFG["strategy"])
    assert len(sigs) == 1 and sigs[0]["stop"] < sigs[0]["close"] < sigs[0]["target"]


def test_backtest_runs_and_is_consistent():
    data = {s: synthetic(s, 500, seed=i) for i, s in enumerate(["A", "B", "C", "D", "E", "F"])}
    res = backtest(data, CFG)
    m = res["metrics"]
    assert m["trades"] >= 0 and -100 <= m["max_drawdown_pct"] <= 0
    t = res["trades"]
    if not t.empty:
        # reported equity must equal start capital + sum of trade pnl (all positions closed at the end)
        assert abs(res["equity"].iloc[-1] - (CFG["risk"]["starting_capital"] + t["pnl"].sum())) < 1.0
        assert (t["qty"] > 0).all()


def test_paper_broker_roundtrip(tmp_path):
    b = PaperBroker(CFG["risk"], path=str(tmp_path / "p.json"))
    start = b.s["cash"]
    b.buy("X", 100, 100.0, 95.0, 110.0)
    assert "X" in b.positions() and b.s["cash"] < start
    b.sell("X", 100.0, "test")
    assert "X" not in b.positions()
    assert 0 < start - b.s["cash"] < 100  # only costs lost on a flat round trip
    b2 = PaperBroker(CFG["risk"], path=str(tmp_path / "p.json"))  # persisted
    assert b2.s["cash"] == b.s["cash"]


def test_review_fallback_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = review_candidates([{"symbol": "X"}], {}, {}, CFG["claude_review"])
    assert out[0]["action"] == "APPROVE"
    assert review_candidates([], {}, {}, CFG["claude_review"]) == []
