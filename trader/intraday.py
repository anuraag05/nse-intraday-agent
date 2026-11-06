"""Intraday opening-range-breakout (ORB) strategy on 5-minute candles + backtester.

Signal (evaluated on a candle's CLOSE, entry on the next candle):
  LONG : close > opening-range high AND close > VWAP AND volume > mult x avg volume AND OR width in range
  SHORT: close < opening-range low  AND close < VWAP AND same volume/width filters (if allow_short)
Stop  : tighter of (opposite side of opening range, ATR-based stop). Target: reward_risk x risk.
One trade per symbol per day. No entries after entry_cutoff. Everything squared off at squareoff_time.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .backtest import metrics
from .risk import can_open, position_size


def _mins(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def add_indicators(df: pd.DataFrame, p: dict) -> pd.DataFrame:
    df = df.copy()
    idx = df.index
    day = idx.normalize()
    mins = np.asarray(idx.hour * 60 + idx.minute)

    tp = (df["high"] + df["low"] + df["close"]) / 3
    df["vwap"] = (tp * df["volume"]).groupby(day).cumsum() / df["volume"].groupby(day).cumsum()
    prev_close = df["close"].groupby(day).shift(1)  # no overnight gap in true range
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(p["atr_period"], min_periods=5).mean()
    df["vol_avg"] = df["volume"].rolling(20, min_periods=5).mean().shift(1)
    df["vol_ratio"] = df["volume"] / df["vol_avg"]

    or_start = _mins(p["session_start"])
    or_end = or_start + p["or_minutes"]
    in_or = (mins >= or_start) & (mins < or_end)
    need = int(np.ceil(p["or_minutes"] / p["candle_minutes"]))
    or_cnt = pd.Series(in_or.astype(int), index=idx).groupby(day).transform("sum")
    df["or_high"] = df["high"].where(in_or).groupby(day).transform("max").where(or_cnt >= need)
    df["or_low"] = df["low"].where(in_or).groupby(day).transform("min").where(or_cnt >= need)

    width = (df["or_high"] - df["or_low"]) / df["close"]
    window = (mins >= or_end) & (mins <= _mins(p["entry_cutoff"]))
    base = (
        window & (width >= p["min_or_pct"] / 100) & (width <= p["max_or_pct"] / 100)
        & (df["volume"] > p["volume_mult"] * df["vol_avg"])
    )
    df["long_stop"] = np.maximum(df["or_low"], df["close"] - p["atr_stop_mult"] * df["atr"])
    df["short_stop"] = np.minimum(df["or_high"], df["close"] + p["atr_stop_mult"] * df["atr"])
    min_d = p["min_stop_pct"] / 100 * df["close"]
    df["long_sig"] = base & (df["close"] > df["or_high"]) & (df["close"] > df["vwap"]) \
        & ((df["close"] - df["long_stop"]) >= min_d)
    df["short_sig"] = (
        base & bool(p.get("allow_short", True)) & (df["close"] < df["or_low"]) & (df["close"] < df["vwap"])
        & ((df["short_stop"] - df["close"]) >= min_d)
    )
    return df


def signal_from_row(row: pd.Series, p: dict) -> dict | None:
    """Trade plan from an indicator row (the signal candle), or None."""
    rr = p["reward_risk"]
    c = float(row["close"])
    if bool(row["long_sig"]):
        stop = float(row["long_stop"])
        return {"side": "long", "close": c, "stop": stop, "target": c + rr * (c - stop),
                "vol_ratio": float(row["vol_ratio"])}
    if bool(row["short_sig"]):
        stop = float(row["short_stop"])
        return {"side": "short", "close": c, "stop": stop, "target": c - rr * (stop - c),
                "vol_ratio": float(row["vol_ratio"])}
    return None


def check_exit(side: str, o: float, h: float, l: float, stop: float, target: float):
    """(raw_exit_price, reason) if the candle hits stop/target, else None. Stop wins if both are hit."""
    if side == "long":
        if l <= stop:
            return min(o, stop), "stop"
        if h >= target:
            return max(o, target), "target"
    else:
        if h >= stop:
            return max(o, stop), "stop"
        if l <= target:
            return min(o, target), "target"
    return None


def backtest_intraday(data: dict[str, pd.DataFrame], cfg: dict) -> dict:
    ip, rk = cfg["intraday"], cfg["risk"]
    cost, slip = rk["cost_pct_per_side"] / 100, rk["slippage_pct"] / 100
    sq = _mins(ip["squareoff_time"])
    ind = {s: add_indicators(df, ip) for s, df in data.items()}
    by_day = {s: {d: g for d, g in df.groupby(df.index.normalize())} for s, df in ind.items()}
    days = sorted({d for m in by_day.values() for d in m})

    capital = float(rk["starting_capital"])
    trades: list[dict] = []
    eq_points: list[tuple] = []

    for day in days:
        bars = {s: m[day] for s, m in by_day.items() if day in m}
        stamps = sorted(set().union(*[b.index for b in bars.values()]))
        pos: dict[str, dict] = {}
        pending: dict[str, dict] = {}
        traded: set[str] = set()
        last_close: dict[str, float] = {}
        st = {"realized": 0.0, "n": 0}
        day_start = capital

        def equity() -> float:
            u = 0.0
            for s, p in pos.items():
                c = last_close.get(s, p["entry"])
                u += (c - p["entry"]) * p["qty"] if p["side"] == "long" else (p["entry"] - c) * p["qty"]
            return capital + st["realized"] + u

        def close(s: str, raw: float, ts, reason: str):
            p = pos.pop(s)
            fill = raw * (1 - slip) if p["side"] == "long" else raw * (1 + slip)
            gross = (fill - p["entry"]) * p["qty"] if p["side"] == "long" else (p["entry"] - fill) * p["qty"]
            pnl = gross - p["qty"] * (p["entry"] + fill) * cost
            st["realized"] += pnl
            trades.append({"symbol": s, "side": p["side"], "entry_ts": p["ts"], "exit_ts": ts, "qty": p["qty"],
                           "entry": round(p["entry"], 2), "exit": round(fill, 2), "pnl": round(pnl, 2),
                           "reason": reason})

        for ts in stamps:
            mins = ts.hour * 60 + ts.minute

            # 1) fill entries decided on the previous candle at this candle's open
            for s, sig in list(pending.items()):
                pending.pop(s)
                if ts not in bars[s].index:
                    continue
                eq = equity()
                ok, _ = can_open(len(pos), eq, day_start, rk)
                if not ok or st["n"] >= rk["max_trades_per_day"]:
                    continue
                o = float(bars[s].at[ts, "open"])
                side = sig["side"]
                entry = o * (1 + slip) if side == "long" else o * (1 - slip)
                bad = (entry <= sig["stop"] or entry >= sig["target"]) if side == "long" \
                    else (entry >= sig["stop"] or entry <= sig["target"])
                if bad:
                    continue
                qty = position_size(eq, entry, sig["stop"], rk, side)
                if qty < 1:
                    continue
                pos[s] = {"side": side, "qty": qty, "entry": entry, "stop": sig["stop"],
                          "target": sig["target"], "ts": ts}
                st["n"] += 1

            # 2) manage open positions on this candle
            for s in list(pos):
                if ts not in bars[s].index:
                    continue
                b, p = bars[s].loc[ts], pos[s]
                if mins >= sq:
                    close(s, float(b["open"]), ts, "squareoff")
                    continue
                hit = check_exit(p["side"], float(b["open"]), float(b["high"]), float(b["low"]), p["stop"], p["target"])
                if hit:
                    close(s, hit[0], ts, hit[1])

            for s, b in bars.items():
                if ts in b.index:
                    last_close[s] = float(b.at[ts, "close"])

            # 3) look for signals on this candle's close (strongest volume first)
            if mins < sq:
                found = []
                for s, b in bars.items():
                    if ts in b.index and s not in pos and s not in traded:
                        sig = signal_from_row(b.loc[ts], ip)
                        if sig:
                            found.append((s, sig))
                for s, sig in sorted(found, key=lambda x: x[1]["vol_ratio"], reverse=True):
                    pending[s] = sig
                    traded.add(s)

        for s in list(pos):  # safety: anything left at the end of the day
            close(s, last_close[s], stamps[-1], "eod")
        capital += st["realized"]
        eq_points.append((day, capital))

    eq = pd.Series({d: e for d, e in eq_points})
    return {"trades": pd.DataFrame(trades), "equity": eq, "metrics": metrics(eq, trades, rk)}


def synthetic_intraday(symbol: str, days: int = 40, seed: int | None = None) -> pd.DataFrame:
    """Random 5-minute OHLCV (75 candles/day, 09:15-15:25). For tests/demos only."""
    import zlib

    rng = np.random.default_rng(seed if seed is not None else zlib.crc32(symbol.encode()))
    n = 75
    u = 1 + 2.2 * (np.abs(np.linspace(-1, 1, n)) ** 3)  # U-shaped volume
    rows, idx, price = [], [], 1000.0
    for d in pd.bdate_range(end=pd.Timestamp.today().normalize() - pd.Timedelta(days=1), periods=days):
        price *= 1 + rng.normal(0, 0.006)
        drift = rng.choice([-1, 0, 1]) * 0.00018
        for i in range(n):
            o = price
            r = drift + rng.normal(0, 0.0014)
            c = o * (1 + r)
            h = max(o, c) * (1 + abs(rng.normal(0, 0.0006)))
            l = min(o, c) * (1 - abs(rng.normal(0, 0.0006)))
            v = rng.lognormal(10.5, 0.4) * u[i] * (3 if abs(r) > 0.003 else 1)
            rows.append((o, h, l, c, v))
            idx.append(d + pd.Timedelta(hours=9, minutes=15 + 5 * i))
            price = c
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=pd.DatetimeIndex(idx))
