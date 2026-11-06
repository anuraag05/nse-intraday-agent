"""Load 5-minute candles for backtests: Dhan (real), CSV files, or synthetic demo data."""
from __future__ import annotations

import pandas as pd


def load_intraday(cfg: dict, days: int | None = None, progress=None, source: str | None = None) -> dict[str, pd.DataFrame]:
    """progress(i, n, symbol) is called after each symbol, if given."""
    from .dhan_client import fetch_history, make_dhan, resolve_security_ids
    from .intraday import synthetic_intraday

    src = source or cfg["data"]["source"]
    syms = cfg["universe"]
    days = days or cfg["data"]["backtest_days"]
    out: dict[str, pd.DataFrame] = {}
    dhan = ids = None
    if src == "dhan":
        dhan = make_dhan()
        ids = resolve_security_ids(dhan, syms, cfg.get("dhan", {}).get("security_ids"))
    for i, s in enumerate(syms, 1):
        try:
            if src == "synthetic":
                out[s] = synthetic_intraday(s, max(int(days * 5 / 7), 5))
            elif src == "csv":
                out[s] = pd.read_csv(f"{cfg['data']['csv_dir']}/{s}.csv", parse_dates=["ts"], index_col="ts").sort_index()
            else:
                df = fetch_history(dhan, ids[s], days, cfg["intraday"]["candle_minutes"])
                if len(df):
                    out[s] = df
        except Exception as e:
            print(f"[data] {s}: {e}")
        if progress:
            progress(i, len(syms), s)
    return out
