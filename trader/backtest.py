"""Event-driven daily backtest of the strategy with costs, slippage and the same risk rules as live.

Timing: signal on day t's close -> entry at day t+1 open. Stops/targets are checked against
day highs/lows (stop assumed hit first if both fall in one bar). Trend exits fill at next open.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .risk import can_open, position_size
from .strategy import add_indicators


def backtest(data: dict[str, pd.DataFrame], cfg: dict) -> dict:
    sp, rk = cfg["strategy"], cfg["risk"]
    cost, slip = rk["cost_pct_per_side"] / 100, rk["slippage_pct"] / 100
    ind = {s: add_indicators(df, sp) for s, df in data.items()}
    dates = sorted(set().union(*[df.index for df in ind.values()]))

    cash = float(rk["starting_capital"])
    pos: dict[str, dict] = {}
    pending_entries: list[dict] = []
    pending_exits: set[str] = set()
    last_close: dict[str, float] = {}
    trades: list[dict] = []
    equity_curve: list[tuple] = []
    prev_equity = cash

    def equity_now() -> float:
        return cash + sum(p["qty"] * last_close.get(s, p["entry"]) for s, p in pos.items())

    def close_pos(sym: str, price: float, d, reason: str):
        nonlocal cash
        p = pos.pop(sym)
        fill = price * (1 - slip)
        cash += p["qty"] * fill * (1 - cost)
        pnl = p["qty"] * fill * (1 - cost) - p["qty"] * p["entry"] * (1 + cost)
        trades.append(
            {"symbol": sym, "entry_date": p["date"], "exit_date": d, "qty": p["qty"],
             "entry": round(p["entry"], 2), "exit": round(fill, 2), "pnl": round(pnl, 2),
             "reason": reason}
        )

    for d in dates:
        # 1) trend exits flagged yesterday fill at today's open
        for sym in list(pending_exits):
            if sym in pos and d in ind[sym].index:
                close_pos(sym, float(ind[sym].at[d, "open"]), d, "trend")
        pending_exits.clear()

        # 2) entries flagged yesterday fill at today's open
        for sig in pending_entries:
            sym = sig["symbol"]
            if sym in pos or d not in ind[sym].index:
                continue
            eq = equity_now()
            ok, _ = can_open(len(pos), eq, prev_equity, rk)
            if not ok:
                continue
            entry = float(ind[sym].at[d, "open"]) * (1 + slip)
            if entry <= sig["stop"]:
                continue
            qty = position_size(eq, entry, sig["stop"], rk)
            qty = min(qty, int(cash / (entry * (1 + cost))))
            if qty < 1:
                continue
            cash -= qty * entry * (1 + cost)
            pos[sym] = {"qty": qty, "entry": entry, "stop": sig["stop"], "target": sig["target"], "date": d}
        pending_entries = []

        # 3) intraday stop / target checks
        for sym in list(pos):
            if d not in ind[sym].index:
                continue
            bar = ind[sym].loc[d]
            p = pos[sym]
            if bar["low"] <= p["stop"]:
                close_pos(sym, min(float(bar["open"]), p["stop"]), d, "stop")
            elif bar["high"] >= p["target"]:
                close_pos(sym, max(float(bar["open"]), p["target"]), d, "target")

        # 4) mark to market
        for sym, df in ind.items():
            if d in df.index:
                last_close[sym] = float(df.at[d, "close"])
        eq = equity_now()
        equity_curve.append((d, eq))

        # 5) end-of-day decisions for tomorrow
        for sym in pos:
            if d in ind[sym].index and ind[sym].at[d, "close"] < ind[sym].at[d, "ema_fast"]:
                pending_exits.add(sym)
        cands = []
        for sym, df in ind.items():
            if d in df.index and sym not in pos and bool(df.at[d, "signal"]):
                row = df.loc[d]
                if row["stop"] < row["close"]:
                    cands.append({"symbol": sym, "stop": float(row["stop"]), "target": float(row["target"]),
                                  "vr": float(row["vol_ratio"])})
        pending_entries = sorted(cands, key=lambda c: c["vr"], reverse=True)
        prev_equity = eq

    # close any open positions at final mark for reporting
    final_day = dates[-1]
    for sym in list(pos):
        close_pos(sym, last_close[sym], final_day, "end_of_test")
    eq_series = pd.Series({d: e for d, e in equity_curve})
    if not eq_series.empty:
        eq_series.iloc[-1] = cash
    return {"trades": pd.DataFrame(trades), "equity": eq_series, "metrics": metrics(eq_series, trades, rk)}


def metrics(eq: pd.Series, trades: list[dict], rk: dict) -> dict:
    if eq.empty:
        return {}
    start = rk["starting_capital"]
    total = eq.iloc[-1] / start - 1
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    daily = eq.pct_change().dropna()
    dd = (eq / eq.cummax() - 1).min()
    pnls = np.array([t["pnl"] for t in trades]) if trades else np.array([])
    wins, losses = pnls[pnls > 0], pnls[pnls <= 0]
    return {
        "total_return_pct": round(total * 100, 2),
        "cagr_pct": round(((1 + total) ** (1 / years) - 1) * 100, 2) if (total > -1 and years >= 0.25) else None,
        "max_drawdown_pct": round(dd * 100, 2),
        "sharpe": round(float(daily.mean() / daily.std() * np.sqrt(252)), 2) if len(daily) > 2 and daily.std() > 0 else 0.0,
        "trades": int(len(pnls)),
        "win_rate_pct": round(len(wins) / len(pnls) * 100, 1) if len(pnls) else 0.0,
        "profit_factor": round(wins.sum() / abs(losses.sum()), 2) if len(losses) and losses.sum() != 0 else None,
        "avg_trade_inr": round(float(pnls.mean()), 2) if len(pnls) else 0.0,
    }
