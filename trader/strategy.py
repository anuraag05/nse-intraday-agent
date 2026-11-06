"""Trend-following breakout strategy (long only, end-of-day).

Entry signal on a day's close:
  close > highest high of previous N days  AND  EMA(fast) > EMA(slow)  AND  close > EMA(fast)
  AND volume > volume_mult x 20-day average volume
Exit: stop (close - k*ATR at signal), target (rr x risk), or close back below EMA(fast).
"""
from __future__ import annotations

import pandas as pd


def add_indicators(df: pd.DataFrame, p: dict) -> pd.DataFrame:
    df = df.copy()
    df["ema_fast"] = df["close"].ewm(span=p["ema_fast"], adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=p["ema_slow"], adjust=False).mean()
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    df["atr"] = tr.rolling(p["atr_period"]).mean()
    df["vol_avg"] = df["volume"].rolling(20).mean()
    df["prior_high"] = df["high"].rolling(p["breakout_lookback"]).max().shift(1)

    df["signal"] = (
        (df["close"] > df["prior_high"])
        & (df["ema_fast"] > df["ema_slow"])
        & (df["close"] > df["ema_fast"])
        & (df["volume"] > p["volume_mult"] * df["vol_avg"])
    )
    df["stop"] = df["close"] - p["atr_stop_mult"] * df["atr"]
    df["target"] = df["close"] + p["reward_risk"] * (df["close"] - df["stop"])
    df["vol_ratio"] = df["volume"] / df["vol_avg"]
    return df


def latest_signals(data: dict[str, pd.DataFrame], p: dict) -> list[dict]:
    """Signals firing on the most recent bar of each symbol, strongest volume first."""
    out = []
    for sym, df in data.items():
        if len(df) < max(p["ema_slow"], p["breakout_lookback"]) + 5:
            continue
        ind = add_indicators(df, p)
        row = ind.iloc[-1]
        if bool(row["signal"]) and row["stop"] < row["close"]:
            out.append(
                {
                    "symbol": sym,
                    "date": str(ind.index[-1].date()),
                    "close": round(float(row["close"]), 2),
                    "stop": round(float(row["stop"]), 2),
                    "target": round(float(row["target"]), 2),
                    "atr": round(float(row["atr"]), 2),
                    "vol_ratio": round(float(row["vol_ratio"]), 2),
                }
            )
    return sorted(out, key=lambda s: s["vol_ratio"], reverse=True)


def should_exit(row: pd.Series, pos: dict) -> str | None:
    """Exit reason for an open position given the latest bar, or None."""
    if row["close"] <= pos["stop"]:
        return "stop"
    if row["close"] >= pos["target"]:
        return "target"
    if row["close"] < row["ema_fast"]:
        return "trend"
    return None
