"""Price data loading: yfinance (NSE '.NS'), local CSV files, or synthetic data for tests."""
from __future__ import annotations

import os
import zlib
from datetime import date, timedelta

import numpy as np
import pandas as pd

COLS = ["open", "high", "low", "close", "volume"]


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)
    df = df[COLS].dropna()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df.index.name = "date"
    return df[~df.index.duplicated()].sort_index()


def load_yfinance(symbol: str, start: str) -> pd.DataFrame:
    import yfinance as yf  # pip install yfinance

    df = yf.download(f"{symbol}.NS", start=start, auto_adjust=True, progress=False)
    if df is None or df.empty:
        raise ValueError(f"No data returned for {symbol}")
    return _clean(df)


def load_csv(symbol: str, csv_dir: str) -> pd.DataFrame:
    path = os.path.join(csv_dir, f"{symbol}.csv")
    df = pd.read_csv(path, parse_dates=["date"], index_col="date")
    return _clean(df)


def synthetic(symbol: str, days: int = 500, seed: int | None = None) -> pd.DataFrame:
    """Random-walk OHLCV with drifting regimes, deterministic per symbol. For tests/demos only."""
    rng = np.random.default_rng(seed if seed is not None else zlib.crc32(symbol.encode()))
    regime = np.repeat(rng.choice([-0.0004, 0.0, 0.0012], size=days // 60 + 1), 60)[:days]
    rets = regime + rng.normal(0, 0.014, days)
    close = 1000 * np.exp(np.cumsum(rets))
    open_ = close * (1 + rng.normal(0, 0.003, days))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.006, days)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.006, days)))
    vol = rng.lognormal(13, 0.35, days) * (1 + 2.5 * (np.abs(rets) > 0.02))
    idx = pd.bdate_range(end=date.today(), periods=days)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol}, index=idx
    )


def load_prices(symbols: list[str], cfg: dict) -> dict[str, pd.DataFrame]:
    src = cfg["source"]
    start = (date.today() - timedelta(days=cfg.get("history_days", 400))).isoformat()
    out: dict[str, pd.DataFrame] = {}
    for s in symbols:
        try:
            if src == "yfinance":
                out[s] = load_yfinance(s, start)
            elif src == "csv":
                out[s] = load_csv(s, cfg["csv_dir"])
            elif src == "synthetic":
                out[s] = synthetic(s, cfg.get("history_days", 400))
            else:
                raise ValueError(f"Unknown data source: {src}")
        except Exception as e:  # keep going; one bad symbol shouldn't stop the run
            print(f"[data] skipping {s}: {e}")
    return out
