"""Walk-forward research: does this strategy have an edge on 5-minute data, and if so which settings?

How it avoids fooling itself
  * Every candidate setting is scored in R-multiples (profit / amount risked) AFTER costs and slippage.
  * Settings are chosen on a TRAIN window and judged only on the following TEST window that the choice
    never saw. Walking forward through the data repeats this, so the out-of-sample (OOS) result measures
    the whole "search then trade" procedure, not a lucky in-sample fit.
  * Scores penalise noise (mean R minus its standard error) and need a minimum number of trades.
  * The verdict uses a day-clustered bootstrap confidence interval, because trades on the same day are correlated.
An EDGE FOUND verdict needs a positive OOS mean AND a confidence interval above zero. Anything weaker is
reported as UNCERTAIN or NO EDGE, and nothing is recommended for live use.
"""
from __future__ import annotations

import copy
import itertools
import json
import os
from datetime import datetime

import numpy as np
import pandas as pd

from .intraday import _mins, apply_signals, base_indicators

GRID = {
    "or_minutes": [15, 30],
    "volume_mult": [1.0, 1.5, 2.0],
    "atr_stop_mult": [1.0, 1.5, 2.5],
    "reward_risk": [1.0, 1.5, 2.0],
    "allow_short": [True, False],
    "entry_cutoff": ["11:30", "14:30"],
}
LEARNED_PATH = os.path.join("state", "learned.json")


# --------------------------------------------------------------------------- trade simulation
def simulate_trades(sym: str, b: pd.DataFrame, p: dict, rk: dict) -> pd.DataFrame:
    """One trade per day (first signal), entered at the next candle's open, exited at stop / target / square-off.
    Same fill rules as the portfolio backtester, but independent of portfolio limits so settings compare fairly."""
    idx = b.index
    day = np.asarray(idx.normalize().asi8)
    mins = np.asarray(idx.hour * 60 + idx.minute)
    o, h, l, c = (b[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    sig_l, sig_s = b["long_sig"].to_numpy(bool), b["short_sig"].to_numpy(bool)
    flat = np.flatnonzero(sig_l | sig_s)
    if len(flat) == 0:
        return pd.DataFrame()
    _, first = np.unique(day[flat], return_index=True)
    cand = flat[first]
    starts = np.r_[0, np.flatnonzero(np.diff(day)) + 1]
    ends = np.r_[starts[1:], len(day)]
    dpos = np.searchsorted(starts, cand, side="right") - 1
    ls, ss = b["long_stop"].to_numpy(float), b["short_stop"].to_numpy(float)
    vr, orh, orl = b["vol_ratio"].to_numpy(float), b["or_high"].to_numpy(float), b["or_low"].to_numpy(float)
    sq, rr = _mins(p["squareoff_time"]), p["reward_risk"]
    cost, slip = rk["cost_pct_per_side"] / 100, rk["slippage_pct"] / 100
    rows = []
    for i, dp in zip(cand, dpos):
        e, j = ends[dp], i + 1
        if j >= e or mins[j] >= sq:
            continue
        long = bool(sig_l[i])
        stop = ls[i] if long else ss[i]
        entry = o[j] * (1 + slip) if long else o[j] * (1 - slip)
        risk_sig = abs(c[i] - stop)
        target = c[i] + rr * risk_sig if long else c[i] - rr * risk_sig
        if (long and (entry <= stop or entry >= target)) or (not long and (entry >= stop or entry <= target)):
            continue
        rel = np.flatnonzero(mins[j:e] >= sq)
        k_end = j + int(rel[0]) if len(rel) else e
        lo, hi = l[j:k_end], h[j:k_end]
        hs = np.flatnonzero((lo <= stop) if long else (hi >= stop))
        ht = np.flatnonzero((hi >= target) if long else (lo <= target))
        fs = hs[0] if len(hs) else 10**9
        ft = ht[0] if len(ht) else 10**9
        if fs <= ft and fs < 10**9:
            raw, reason = (min(o[j + fs], stop) if long else max(o[j + fs], stop)), "stop"
        elif ft < 10**9:
            raw, reason = (max(o[j + ft], target) if long else min(o[j + ft], target)), "target"
        else:
            raw, reason = (o[k_end] if k_end < e else c[e - 1]), "squareoff"
        fill = raw * (1 - slip) if long else raw * (1 + slip)
        net = ((fill - entry) if long else (entry - fill)) - cost * (entry + fill)
        risk_ps = abs(entry - stop)
        rows.append((sym, day[i], "long" if long else "short", int(mins[j]), net / risk_ps, reason,
                     vr[i], (orh[i] - orl[i]) / c[i] * 100, risk_ps / entry * 100))
    return pd.DataFrame(rows, columns=["symbol", "day", "side", "entry_min", "R", "reason", "vol_ratio",
                                       "or_width_pct", "stop_pct"])


def _combos(cfg_ip: dict, grid: dict) -> list[dict]:
    g = {k: list(v) for k, v in grid.items()}
    for k in g:  # always include the currently configured value so the baseline is part of the search
        if cfg_ip.get(k) is not None and cfg_ip[k] not in g[k]:
            g[k].append(cfg_ip[k])
    keys = list(g)
    return [dict(zip(keys, vals)) for vals in itertools.product(*g.values())]


def collect_trades(data: dict[str, pd.DataFrame], cfg: dict, grid: dict = GRID, progress=None) -> tuple[pd.DataFrame, list[dict]]:
    ip0, rk = cfg["intraday"], cfg["risk"]
    combos = _combos(ip0, grid)
    frames = []
    by_or: dict[int, list[int]] = {}
    for cid, cb in enumerate(combos):
        by_or.setdefault(cb["or_minutes"], []).append(cid)
    done, total = 0, len(combos) * len(data)
    for or_min, cids in by_or.items():
        p_base = {**ip0, "or_minutes": or_min}
        base = {s: base_indicators(df, p_base) for s, df in data.items()}
        for cid in cids:
            p = {**ip0, **combos[cid]}
            for s, b in base.items():
                t = simulate_trades(s, apply_signals(b, p), p, rk)
                if len(t):
                    t["combo"] = cid
                    frames.append(t)
                done += 1
            if progress:
                progress(done, total)
    trades = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["symbol", "day", "side", "entry_min", "R", "reason", "vol_ratio", "or_width_pct", "stop_pct", "combo"])
    return trades, combos


# --------------------------------------------------------------------------- statistics
def _score(g: pd.core.groupby.DataFrameGroupBy, min_trades: int) -> pd.Series:
    n, m, sd = g["R"].size(), g["R"].mean(), g["R"].std(ddof=1).fillna(0)
    sc = m - sd / np.sqrt(n.clip(lower=1))
    return sc.where(n >= min_trades, -np.inf)


def cluster_bootstrap(trades: pd.DataFrame, n_boot: int = 2000, seed: int = 0) -> dict:
    """Mean R with a 95% CI, resampling whole DAYS (trades on one day are not independent)."""
    if trades.empty:
        return {"n": 0, "mean_R": 0.0, "ci_low": 0.0, "ci_high": 0.0, "win_rate": 0.0, "total_R": 0.0}
    g = trades.groupby("day")["R"].agg(["sum", "count"])
    s, n = g["sum"].to_numpy(), g["count"].to_numpy()
    rng = np.random.default_rng(seed)
    pick = rng.integers(0, len(g), (n_boot, len(g)))
    means = s[pick].sum(1) / n[pick].sum(1)
    return {"n": int(n.sum()), "mean_R": float(s.sum() / n.sum()), "ci_low": float(np.percentile(means, 2.5)),
            "ci_high": float(np.percentile(means, 97.5)), "win_rate": float((trades["R"] > 0).mean() * 100),
            "total_R": float(trades["R"].sum())}


def walk_forward(trades: pd.DataFrame, all_days: list, min_trades: int = 40, base_combo: int | None = None) -> dict:
    """Roll through `all_days`: choose a setting on `train` days, judge it on the next `test` days."""
    n_days = len(all_days)
    train = int(min(60, max(25, n_days // 2)))
    test = int(min(20, max(8, n_days // 6)))
    folds, oos_parts, base_parts, start = [], [], [], 0
    while start + train + test <= n_days:
        tr_days, te_days = set(all_days[start:start + train]), set(all_days[start + train:start + train + test])
        sc = _score(trades[trades["day"].isin(tr_days)].groupby("combo"), min_trades)
        if len(sc) and np.isfinite(sc.max()):
            best = int(sc.idxmax())
            te = trades[(trades["combo"] == best) & trades["day"].isin(te_days)]
            oos_parts.append(te)
            if base_combo is not None:
                base_parts.append(trades[(trades["combo"] == base_combo) & trades["day"].isin(te_days)])
            folds.append({"fold": len(folds) + 1, "combo": best, "train_score": float(sc.max()),
                          "test_trades": int(len(te)), "test_mean_R": float(te["R"].mean()) if len(te) else 0.0})
        start += test
    oos = pd.concat(oos_parts) if oos_parts else trades.iloc[0:0]
    base = pd.concat(base_parts) if base_parts else trades.iloc[0:0]
    return {"folds": pd.DataFrame(folds), "oos": oos, "oos_stats": cluster_bootstrap(oos),
            "baseline_stats": cluster_bootstrap(base), "train_days": train, "test_days": test}


def verdict(stats: dict, n_folds: int) -> tuple[str, str]:
    if n_folds < 1 or stats["n"] < 100:
        return "NOT ENOUGH DATA", (f"Only {stats['n']} out-of-sample trades over {n_folds} fold(s). Need at least 100 "
                                   "(roughly 3+ months of 5-minute data across ~15 stocks).")
    m, lo = stats["mean_R"], stats["ci_low"]
    if m >= 0.05 and lo > 0:
        return "EDGE FOUND", (f"Out-of-sample average {m:+.2f}R per trade after costs (95% range {lo:+.2f} to "
                              f"{stats['ci_high']:+.2f}). Still paper-trade it before risking money.")
    if m > 0:
        return "UNCERTAIN", (f"Out-of-sample average {m:+.2f}R, but the 95% range ({lo:+.2f} to {stats['ci_high']:+.2f}) "
                             "includes zero, so it could be luck. Do not trade it live; gather more data.")
    return "NO EDGE", (f"Out-of-sample average {m:+.2f}R per trade after costs. Tuning did not find settings that make "
                       "money on data they hadn't seen. Don't trade this strategy.")


# --------------------------------------------------------------------------- insights
def _bucket_table(df: pd.DataFrame, key: pd.Series, label: str) -> pd.DataFrame:
    g = df.groupby(key, observed=True)["R"]
    out = pd.DataFrame({"trades": g.size(), "win_%": g.apply(lambda x: (x > 0).mean() * 100).round(1),
                        "mean_R": g.mean().round(3), "total_R": g.sum().round(1),
                        "se": (g.std(ddof=1) / np.sqrt(g.size())).round(3)})
    out.index.name = label
    return out


def insights(trades: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """What works / doesn't, measured on ONE fixed setting (no selection bias)."""
    if trades.empty:
        return {}
    t = trades.copy()
    t["time"] = (t["entry_min"] // 30 * 30).map(lambda m: f"{m // 60:02d}:{m % 60:02d}")
    out = {"side": _bucket_table(t, t["side"], "side"), "exit": _bucket_table(t, t["reason"], "exit"),
           "entry time (30-min)": _bucket_table(t, t["time"], "entry time")}
    for col, name in [("vol_ratio", "breakout volume vs avg"), ("or_width_pct", "opening range width %"),
                      ("stop_pct", "stop distance %")]:
        try:
            q = pd.qcut(t[col], 4, duplicates="drop")
            out[name] = _bucket_table(t, q.astype(str), name)
        except ValueError:
            pass
    return out


def notes_from_insights(ins: dict[str, pd.DataFrame], min_n: int = 30) -> list[str]:
    notes = []
    for name, tab in ins.items():
        for k, r in tab.iterrows():
            if r["trades"] >= min_n and r["se"] > 0:
                z = r["mean_R"] / r["se"]
                if z >= 2:
                    notes.append(f"{name} = {k}: works ({r['mean_R']:+.2f}R avg over {int(r['trades'])} trades)")
                elif z <= -2:
                    notes.append(f"{name} = {k}: loses ({r['mean_R']:+.2f}R avg over {int(r['trades'])} trades)")
    return notes


# --------------------------------------------------------------------------- main entry
def run(data: dict[str, pd.DataFrame], cfg: dict, grid: dict = GRID, progress=None) -> dict:
    trades, combos = collect_trades(data, cfg, grid, progress)
    all_days = sorted({int(v) for df in data.values() for v in np.unique(df.index.normalize().asi8)})
    n_days = len(all_days)
    ip0 = cfg["intraday"]
    base_id = next((i for i, c in enumerate(combos) if all(ip0.get(k) == v for k, v in c.items())), None)
    out = {"n_days": n_days, "n_symbols": len(data), "n_combos": len(combos), "n_trades_all": len(trades)}
    if trades.empty or n_days < 40:
        out.update(verdict="NOT ENOUGH DATA",
                   message=f"{n_days} trading days is too few (need about 40+, ideally 90+) or no trades were generated.",
                   oos_stats=cluster_bootstrap(trades.iloc[0:0]), folds=pd.DataFrame(), insights={}, notes=[],
                   best_params=None, baseline_stats=cluster_bootstrap(trades.iloc[0:0]))
        return out
    wf = walk_forward(trades, all_days, base_combo=base_id)
    v, msg = verdict(wf["oos_stats"], len(wf["folds"]))
    sc_all = _score(trades.groupby("combo"), 40)
    best_id = int(sc_all.idxmax()) if np.isfinite(sc_all.max()) else base_id
    ins_trades = trades[trades["combo"] == base_id] if base_id is not None else trades[trades["combo"] == best_id]
    ins = insights(ins_trades)
    out.update(verdict=v, message=msg, oos_stats=wf["oos_stats"], baseline_stats=wf["baseline_stats"],
               folds=wf["folds"].assign(params=wf["folds"]["combo"].map(lambda i: json.dumps(combos[i]))) if len(wf["folds"]) else wf["folds"],
               train_days=wf["train_days"], test_days=wf["test_days"], insights=ins, notes=notes_from_insights(ins),
               best_params=combos[best_id] if best_id is not None else None,
               best_in_sample=cluster_bootstrap(trades[trades["combo"] == best_id]) if best_id is not None else None,
               current_in_sample=cluster_bootstrap(ins_trades), trades=trades, combos=combos)
    return out


def save_learned(res: dict, path: str = LEARNED_PATH) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    keep = {k: res.get(k) for k in ("verdict", "message", "n_days", "n_symbols", "best_params", "notes", "oos_stats")}
    keep["when"] = datetime.now().isoformat(timespec="seconds")
    with open(path, "w") as f:
        json.dump(keep, f, indent=2, default=float)


def load_learned(path: str = LEARNED_PATH) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def research_notes_for_claude(path: str = LEARNED_PATH) -> str:
    d = load_learned(path)
    if not d:
        return ""
    lines = [f"Backtest research on {d.get('n_days')} days: {d.get('verdict')} - {d.get('message')}"]
    lines += d.get("notes") or []
    return "\n".join(lines)


def applicable(res: dict) -> bool:
    """Only settings that survived out-of-sample testing may be applied automatically."""
    return res.get("verdict") == "EDGE FOUND" and bool(res.get("best_params"))
