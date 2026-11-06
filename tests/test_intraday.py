import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd
import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from trader.dhan_client import parse_candles, resolve_security_ids
from trader.ibroker import DhanBroker, PaperIntradayBroker
from trader.intraday import add_indicators, backtest_intraday, check_exit, signal_from_row, synthetic_intraday
from trader.live import seconds_to_next_cycle
from trader.risk import position_size

CFG = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "..", "config_intraday.yaml")))
IP, RK = CFG["intraday"], CFG["risk"]


def make_day(day="2026-03-02", breakout=True, direction="up"):
    """75 five-minute candles: flat opening range ~100, drift, then a high-volume breakout at 10:00."""
    idx = pd.date_range(f"{day} 09:15", periods=75, freq="5min")
    rng = np.random.default_rng(0)
    close = 100 + rng.normal(0, 0.03, 75)
    vol = np.full(75, 1e5)
    if breakout:
        k = 9  # 10:00 candle
        sgn = 1 if direction == "up" else -1
        close[k:] = 100 + sgn * (0.9 + 0.01 * np.arange(75 - k))
        vol[k] = 5e5
    df = pd.DataFrame({"open": np.r_[close[0], close[:-1]], "high": close + 0.15, "low": close - 0.15,
                       "close": close, "volume": vol}, index=idx)
    return df


def test_orb_long_signal_on_breakout_candle_only():
    d = add_indicators(make_day(), IP)
    sig_rows = d[d["long_sig"]]
    assert len(sig_rows) >= 1 and sig_rows.index[0].strftime("%H:%M") == "10:00"
    s = signal_from_row(d.loc[sig_rows.index[0]], IP)
    assert s["side"] == "long" and s["stop"] < s["close"] < s["target"]
    assert not d["short_sig"].any()


def test_orb_short_signal():
    d = add_indicators(make_day(direction="down"), IP)
    s = signal_from_row(d[d["short_sig"]].iloc[0], IP)
    assert s["side"] == "short" and s["target"] < s["close"] < s["stop"]


def test_no_signal_inside_opening_range_or_without_breakout():
    d = add_indicators(make_day(breakout=False), IP)
    assert not d["long_sig"].any() and not d["short_sig"].any()
    d2 = add_indicators(make_day(), IP)
    assert not d2[d2.index.strftime("%H:%M") < "09:30"]["long_sig"].any()


def test_no_lookahead_in_signals():
    df = synthetic_intraday("LA", 15, seed=5)
    full = add_indicators(df, IP)
    cut = len(df) - 140
    part = add_indicators(df.iloc[:cut], IP)
    for col in ["long_sig", "short_sig", "vwap", "atr"]:
        a, b = full[col].iloc[cut - 1], part[col].iloc[-1]
        assert (a == b) or (isinstance(a, float) and np.isclose(a, b, equal_nan=True))


def test_check_exit_rules():
    assert check_exit("long", 100, 101, 98, 99, 103) == (99, "stop")
    assert check_exit("long", 98, 104, 97, 99, 103) == (98, "stop")      # gap below stop fills at open
    assert check_exit("long", 100, 104, 99.5, 99, 103) == (103, "target")
    assert check_exit("short", 100, 102, 99, 101, 98) == (101, "stop")
    assert check_exit("short", 100, 100.5, 97, 101, 98) == (98, "target")
    assert check_exit("long", 100, 101, 99.5, 99, 103) is None
    assert check_exit("long", 100, 104, 98, 99, 103)[1] == "stop"         # both hit -> stop wins


def test_short_position_size():
    assert position_size(200000, 100, 101, RK, "short") == 1000   # risk 1000 / 1 per share
    assert position_size(200000, 100, 99, RK, "short") == 0       # stop on the wrong side


def test_backtest_squares_off_and_pnl_reconciles():
    data = {s: synthetic_intraday(s, 40, seed=i) for i, s in enumerate("ABCDEFGH")}
    res = backtest_intraday(data, CFG)
    t = res["trades"]
    assert not t.empty, "synthetic data should produce at least one trade"
    assert (t["exit_ts"].dt.strftime("%H:%M") <= "15:25").all()
    assert (t["exit_ts"].dt.normalize() == t["entry_ts"].dt.normalize()).all()      # nothing held overnight
    assert abs(res["equity"].iloc[-1] - (RK["starting_capital"] + t["pnl"].sum())) < 1.0
    per_day = t.groupby(t["entry_ts"].dt.normalize()).size()
    assert per_day.max() <= RK["max_trades_per_day"]


def test_backtest_forced_trade_hits_target():
    data = {"X": make_day()}
    res = backtest_intraday(data, CFG)
    t = res["trades"]
    assert len(t) == 1 and t.iloc[0]["side"] == "long"
    assert t.iloc[0]["reason"] in ("target", "squareoff")


def test_paper_broker_stop_and_squareoff(tmp_path):
    b = PaperIntradayBroker(RK, path=str(tmp_path / "p.json"))
    cap = b.s["capital"]
    b.open("X", "long", 100, 100.0, 99.0, 102.0)
    bars = pd.DataFrame({"open": [100.0], "high": [100.4], "low": [98.5], "close": [99.0], "volume": [1]},
                        index=pd.DatetimeIndex([datetime.now() + pd.Timedelta(minutes=5)]))
    b.update({"X": bars}, {"X": 99.0})
    assert "X" not in b.positions and b.s["capital"] < cap
    b.open("Y", "short", 50, 200.0, 202.0, 196.0)
    b.square_off_all({"Y": 200.0})
    assert not b.positions


class FakeDhan:
    NSE, BUY, SELL, LIMIT, MARKET, INTRA = "NSE_EQ", "BUY", "SELL", "LIMIT", "MARKET", "INTRADAY"

    def __init__(self):
        self.calls, self.net = [], 0

    def place_super_order(self, **kw):
        self.calls.append(("super", kw))
        return {"status": "success", "data": {"orderId": "123", "orderStatus": "PENDING"}}

    def get_positions(self):
        return {"status": "success", "data": [{"securityId": "1333", "productType": "INTRADAY", "netQty": self.net}]}

    def cancel_super_order(self, oid, leg):
        self.calls.append(("cancel", oid, leg))
        return {"status": "success"}

    def place_order(self, **kw):
        self.calls.append(("order", kw))
        self.net = 0
        return {"status": "success"}


def test_dhan_broker_requires_confirm_env(monkeypatch, tmp_path):
    monkeypatch.delenv("LIVE_TRADING_CONFIRM", raising=False)
    with pytest.raises(RuntimeError):
        DhanBroker(FakeDhan(), {"X": "1333"}, RK, CFG["live"], path=str(tmp_path / "l.json"))


def test_dhan_broker_flow(monkeypatch, tmp_path):
    monkeypatch.setenv("LIVE_TRADING_CONFIRM", "YES")
    live = {**CFG["live"], "confirm_each_order": False}
    f = FakeDhan()
    b = DhanBroker(f, {"X": "1333"}, RK, live, path=str(tmp_path / "l.json"))
    r = b.open("X", "long", 10, 100.0, 99.0, 102.0)
    assert r["status"] == "sent"
    kw = f.calls[0][1]
    assert kw["transaction_type"] == "BUY" and kw["product_type"] == "INTRADAY" and kw["quantity"] == 10
    assert kw["stopLossPrice"] == 99.0 and kw["targetPrice"] == 102.0 and kw["price"] > 100.0
    f.net = 10
    b.update({}, {})
    assert b.positions["X"]["state"] == "open"
    f.net = 0
    b.update({}, {})
    assert "X" not in b.positions                       # stop/target executed at broker
    # unfilled entry is cancelled after N cycles
    b.open("X", "long", 10, 100.0, 99.0, 102.0)
    for _ in range(live["cancel_unfilled_after_cycles"]):
        b.update({}, {})
    assert "X" not in b.positions and any(c[0] == "cancel" for c in f.calls)
    # square-off sends an opposite MARKET order only for the remaining net qty
    b.open("X", "short", 10, 100.0, 101.0, 98.0)
    f.net = -10
    b.update({}, {})
    b.square_off_all({})
    last = [c for c in f.calls if c[0] == "order"][-1][1]
    assert last["transaction_type"] == "BUY" and last["quantity"] == 10 and last["order_type"] == "MARKET"
    assert not b.positions


def test_dhan_broker_rejects_bad_short_levels(monkeypatch, tmp_path):
    monkeypatch.setenv("LIVE_TRADING_CONFIRM", "YES")

    class Strict(FakeDhan):
        def place_super_order(self, **kw):
            raise ValueError("For SELL: stopLossPrice must be > price.")

    b = DhanBroker(Strict(), {"X": "1333"}, RK, {**CFG["live"], "confirm_each_order": False}, path=str(tmp_path / "l.json"))
    assert "rejected" in b.open("X", "short", 10, 100.0, 99.0, 98.0)["status"]
    assert not b.positions


def test_parse_candles_and_errors():
    ts = [1772423100, 1772423400]  # 2026-03-02 03:45 / 03:50 UTC -> 09:15 / 09:20 IST
    resp = {"status": "success", "data": {"open": [1, 2], "high": [2, 3], "low": [0.5, 1], "close": [1.5, 2.5],
                                           "volume": [10, 20], "timestamp": ts}}
    df = parse_candles(resp)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"] and len(df) == 2
    assert df.index[0].strftime("%H:%M") == "09:15"
    assert parse_candles({"status": "success", "data": {}}).empty
    with pytest.raises(RuntimeError):
        parse_candles({"status": "failure", "remarks": "boom", "data": ""})
    with pytest.raises(RuntimeError):
        parse_candles({"status": "success", "data": {"open": [1], "high": [1], "low": [1], "close": [1],
                                                      "volume": [1], "timestamp": [100]}})


def test_security_id_overrides_need_no_network():
    assert resolve_security_ids(None, ["A", "B"], {"A": 1, "B": "2"}) == {"A": "1", "B": "2"}


def test_cycle_timing():
    now = datetime(2026, 3, 2, 10, 2, 30)
    assert abs(seconds_to_next_cycle(now, 5, 6) - (2 * 60 + 36)) < 1e-6
    now = datetime(2026, 3, 2, 10, 5, 3)   # just after a boundary -> wait for the next one
    assert abs(seconds_to_next_cycle(now, 5, 6) - (5 * 60 + 3)) < 1e-6
