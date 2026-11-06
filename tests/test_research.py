import json
import os
import sys
import types

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.dirname(__file__))

from test_intraday import CFG, IP, RK, make_day  # noqa: E402
from trader import research
from trader.intraday import apply_signals, base_indicators, synthetic_intraday


def mk(edge, seed0, n=12, days=150):
    return {f"S{i}": synthetic_intraday(f"S{i}", days, seed=seed0 + i, edge=edge, trend=0.0) for i in range(n)}


def test_verdict_rules():
    st = lambda n, m, lo, hi: {"n": n, "mean_R": m, "ci_low": lo, "ci_high": hi}
    assert research.verdict(st(500, 0.2, 0.05, 0.35), 3)[0] == "EDGE FOUND"
    assert research.verdict(st(500, 0.2, -0.05, 0.45), 3)[0] == "UNCERTAIN"
    assert research.verdict(st(500, -0.1, -0.2, 0.0), 3)[0] == "NO EDGE"
    assert research.verdict(st(60, 0.9, 0.5, 1.2), 3)[0] == "NOT ENOUGH DATA"      # too few trades
    assert research.verdict(st(500, 0.03, 0.01, 0.05), 3)[0] != "EDGE FOUND"       # positive but trivially small


def test_simulate_trades_forced_breakout():
    df = pd.concat([make_day(f"2026-02-{d}", breakout=False) for d in (24, 25, 26, 27)] + [make_day("2026-03-02")])
    b = apply_signals(base_indicators(df, IP), IP)
    t = research.simulate_trades("X", b, IP, RK)
    assert len(t) == 1 and t.iloc[0]["side"] == "long" and t.iloc[0]["entry_min"] == 10 * 60 + 5
    assert t.iloc[0]["R"] > 0 and t.iloc[0]["reason"] in ("target", "squareoff")


def test_bootstrap_ci_brackets_mean():
    tr = pd.DataFrame({"day": [1, 1, 2, 2, 3, 3, 4, 4], "R": [1, -1, 1, 1, -1, 1, 1, 1.0]})
    s = research.cluster_bootstrap(tr)
    assert s["ci_low"] <= s["mean_R"] <= s["ci_high"] and s["n"] == 8


def test_finds_planted_edge():
    r = research.run(mk(0.0004, 20000), CFG)
    assert r["verdict"] == "EDGE FOUND", (r["verdict"], r["oos_stats"])
    assert research.applicable(r) and r["best_params"]["allow_short"] in (True, False)
    assert r["oos_stats"]["mean_R"] > 0.2 and r["oos_stats"]["mean_R"] > r["baseline_stats"]["mean_R"]


def test_does_not_hallucinate_edge_on_random_walk():
    r = research.run(mk(0.0, 10000), CFG)
    assert r["verdict"] != "EDGE FOUND" and not research.applicable(r), (r["verdict"], r["oos_stats"])


def test_learned_file_and_claude_notes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert research.research_notes_for_claude() == ""
    res = {"verdict": "NO EDGE", "message": "no luck", "n_days": 100, "n_symbols": 5, "best_params": None,
           "notes": ["side = short: loses (-0.30R avg over 100 trades)"], "oos_stats": {"n": 1, "mean_R": -0.1}}
    research.save_learned(res)
    note = research.research_notes_for_claude()
    assert "NO EDGE" in note and "short" in note
    assert json.load(open("state/learned.json"))["verdict"] == "NO EDGE"
    assert not research.applicable(res)


def test_claude_review_gets_notes_and_obeys_decisions(tmp_path, monkeypatch):
    from trader import agent

    monkeypatch.chdir(tmp_path)
    research.save_learned({"verdict": "UNCERTAIN", "message": "m", "n_days": 90, "n_symbols": 5, "best_params": None,
                           "notes": ["entry time = 13:30: loses"], "oos_stats": {}})
    seen = {}

    class Msg:
        def __init__(self, content):
            self.content = content

    class Block:
        type, name, id = "tool_use", "submit_decisions", "t1"
        input = {"decisions": [{"symbol": "A", "action": "APPROVE", "reason": "ok"}]}

    class Client:
        class messages:
            @staticmethod
            def create(**kw):
                seen["first"] = kw["messages"][0]["content"]
                return Msg([Block()])

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=lambda: Client))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    out = agent.review_candidates([{"symbol": "A"}, {"symbol": "B"}], {}, {}, {"enabled": True, "model": "m", "max_turns": 3})
    assert "entry time = 13:30: loses" in seen["first"]
    assert [d["action"] for d in out] == ["APPROVE", "REJECT"]            # undecided candidate defaults to REJECT
