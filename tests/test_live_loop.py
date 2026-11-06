"""Simulates a whole trading day through run_intraday with a fake clock and a fake Dhan data feed."""
import copy
import json
import os
import sys
from datetime import datetime, timedelta

import pandas as pd
import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.dirname(__file__))

from test_intraday import CFG, FakeDhan, make_day  # noqa: E402
from trader import live


class FeedDhan(FakeDhan):
    """Serves candles from a DataFrame, only those that have STARTED by the 'to' time (like a real feed)."""

    def __init__(self, df, clock):
        super().__init__()
        self.df, self.clock = df, clock

    def intraday_minute_data(self, sid, seg, inst, frm, to, interval=5):
        d = self.df[(self.df.index >= pd.Timestamp(frm)) & (self.df.index <= pd.Timestamp(self.clock.t))]
        ts = ((d.index - pd.Timedelta(hours=5, minutes=30)) - pd.Timestamp("1970-01-01")).total_seconds().astype(int)
        return {"status": "success", "data": {"open": d.open.tolist(), "high": d.high.tolist(), "low": d.low.tolist(),
                                              "close": d.close.tolist(), "volume": d.volume.tolist(),
                                              "timestamp": list(ts)}}


class Clock:
    def __init__(self, t):
        self.t = t


@pytest.fixture()
def sim(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    clock = Clock(datetime(2026, 3, 2, 9, 10, 0))  # a Monday
    days = [make_day(f"2026-02-{d}", breakout=False) for d in (24, 25, 26, 27)] + [make_day("2026-03-02")]
    feed = FeedDhan(pd.concat(days), clock)
    monkeypatch.setattr(live, "now_ist", lambda: clock.t)
    monkeypatch.setattr(live.time, "sleep", lambda s: setattr(clock, "t", clock.t + timedelta(seconds=s)))
    monkeypatch.setattr(live, "make_dhan", lambda: feed)
    cfg = copy.deepcopy(CFG)
    cfg["universe"] = ["X"]
    cfg["dhan"] = {"security_ids": {"X": "1333"}}
    cfg["claude_review"]["enabled"] = False
    return cfg, clock, tmp_path


def test_full_paper_day(sim, capsys):
    cfg, clock, tmp = sim
    live.run_intraday(cfg, live=False)
    out = capsys.readouterr().out
    st = json.loads((tmp / "state" / "intraday_paper.json").read_text())
    kinds = [(r["kind"], r["side"]) for r in st["log"]]
    assert ("OPEN", "long") in kinds and any(k == "CLOSE" for k, _ in kinds), out
    assert st["positions"] == {}                      # flat at the end of the day
    assert st["capital"] != cfg["risk"]["starting_capital"]
    assert clock.t.hour * 60 + clock.t.minute <= 15 * 60 + 30
    opens = [r for r in st["log"] if r["kind"] == "OPEN"]
    assert len(opens) == 1                            # one attempt per symbol per day


def test_stop_file_blocks_entries(sim):
    cfg, clock, tmp = sim
    (tmp / "state").mkdir()
    (tmp / "state" / "STOP").write_text("")
    live.run_intraday(cfg, live=False)
    st = json.loads((tmp / "state" / "intraday_paper.json").read_text())
    assert st["log"] == []


def test_live_flag_requires_live_mode(sim):
    cfg, _, _ = sim
    cfg["mode"] = "paper"
    with pytest.raises(SystemExit):
        live.run_intraday(cfg, live=True)
