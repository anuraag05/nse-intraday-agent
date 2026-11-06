"""Live / paper intraday loop. One cycle per completed 5-minute candle during market hours (IST)."""
from __future__ import annotations

import math
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from .agent import SYSTEM_INTRADAY, review_candidates
from .dhan_client import fetch_candles, make_dhan, resolve_security_ids
from .ibroker import DhanBroker, PaperIntradayBroker
from .intraday import _mins, add_indicators, signal_from_row
from .risk import can_open, position_size

IST = ZoneInfo("Asia/Kolkata")
STOP_FILE = "state/STOP"  # create this file to block new entries immediately
SQUAREOFF_FILE = "state/SQUAREOFF"  # create to close all positions at the next cycle


def now_ist() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def seconds_to_next_cycle(now: datetime, step: int, delay: int) -> float:
    """Seconds until the next `step`-minute boundary plus `delay` seconds."""
    base = now.replace(second=0, microsecond=0)
    boundary = base + timedelta(minutes=step - (base.minute % step))
    return max((boundary + timedelta(seconds=delay) - now).total_seconds(), 0.0)


def fetch_today(dhan, ids: dict, symbols: list[str], interval: int, now: datetime) -> dict[str, pd.DataFrame]:
    """Last few days of candles (for ATR warm-up), keeping only candles that have fully closed."""
    start = (now - timedelta(days=5)).replace(hour=9, minute=15, second=0, microsecond=0)
    out = {}
    for s in symbols:
        try:
            df = fetch_candles(dhan, ids[s], start, now, interval)
        except Exception as e:
            print(f"[data] {s}: {e}")
            continue
        df = df[df.index + pd.Timedelta(minutes=interval) <= pd.Timestamp(now)]
        if len(df):
            out[s] = df
    return out


def fresh_signals(bars: dict, ip: dict, now: datetime, held: set, traded: set) -> tuple[list, dict]:
    """Signals on each symbol's latest closed candle, if that candle just closed. Returns (candidates, indicator frames)."""
    cands, ind = [], {}
    for s, df in bars.items():
        d = add_indicators(df, ip)
        ind[s] = d
        last = d.index[-1]
        age = (pd.Timestamp(now) - (last + pd.Timedelta(minutes=ip["candle_minutes"]))).total_seconds()
        if last.date() != now.date() or age > 2 * 60 * ip["candle_minutes"] or s in held or s in traded:
            continue
        sig = signal_from_row(d.iloc[-1], ip)
        if sig:
            cands.append({"symbol": s, "time": str(last), **{k: (round(v, 2) if isinstance(v, float) else v)
                                                              for k, v in sig.items()}})
    return sorted(cands, key=lambda c: c["vol_ratio"], reverse=True), ind


def run_intraday(cfg: dict, live: bool) -> None:
    ip, rk, lv = cfg["intraday"], cfg["risk"], cfg["live"]
    if live and cfg.get("mode") != "live":
        raise SystemExit("Refusing: --live given but config mode is not 'live'.")
    if cfg.get("mode") == "live" and not live:
        print("[info] config mode is live but --live not passed; running in paper mode.")

    dhan = make_dhan()
    syms = cfg["universe"]
    ids = resolve_security_ids(dhan, syms, cfg.get("dhan", {}).get("security_ids"))
    if os.path.exists(SQUAREOFF_FILE):
        os.remove(SQUAREOFF_FILE)
    if os.path.exists(STOP_FILE):
        print("[info] state/STOP exists: no new entries will be taken until it is removed.")
    broker = DhanBroker(dhan, ids, rk, lv) if live else PaperIntradayBroker(rk)
    base = rk["trading_capital"] if live else broker.s["capital"]
    print(f"Mode: {'LIVE' if live else 'paper'} | symbols: {len(syms)} | capital base: {base:,.0f}")

    open_m, cut_m, sq_m = _mins(ip["session_start"]), _mins(ip["entry_cutoff"]), _mins(ip["squareoff_time"])
    step = ip["candle_minutes"]
    traded: set[str] = set()
    n_trades = 0
    day_start_eq = None

    while True:
        now = now_ist()
        mins = now.hour * 60 + now.minute
        if now.weekday() >= 5 or mins >= 15 * 60 + 30:
            print("Market closed. Done."); return
        # sleep until just after the next candle boundary
        time.sleep(seconds_to_next_cycle(now, step, lv["candle_delay_seconds"]))
        now = now_ist()
        mins = now.hour * 60 + now.minute
        if mins < open_m + ip["or_minutes"]:
            continue  # opening range still forming

        bars = fetch_today(dhan, ids, syms, step, now)
        prices = {s: float(df["close"].iloc[-1]) for s, df in bars.items()}
        broker.update(bars, prices)
        eq = broker.equity(prices)
        day_start_eq = day_start_eq or eq

        if mins >= sq_m or os.path.exists(SQUAREOFF_FILE):
            print(f"{now:%H:%M} square-off"); broker.square_off_all(prices)
            if os.path.exists(SQUAREOFF_FILE):
                os.remove(SQUAREOFF_FILE)
            if not broker.positions:
                print("Flat. Done for the day."); return
            continue

        if mins > cut_m or os.path.exists(STOP_FILE):
            print(f"{now:%H:%M} no new entries ({'STOP file' if os.path.exists(STOP_FILE) else 'after cutoff'}); "
                  f"open: {list(broker.positions)}")
            continue

        cands, ind = fresh_signals(bars, ip, now, set(broker.positions), traded)
        sized = []
        for c in cands:
            traded.add(c["symbol"])  # one attempt per symbol per day
            qty = position_size(eq, c["close"], c["stop"], rk, c["side"])
            if qty >= 1:
                sized.append({**c, "qty": qty})
        if not sized:
            print(f"{now:%H:%M} no actionable signals; open: {list(broker.positions)}"); continue

        pf = {"equity": round(eq), "positions": broker.positions}
        verdicts = {d["symbol"]: d for d in review_candidates(sized, ind, pf, cfg["claude_review"], SYSTEM_INTRADAY)}
        for c in sized:
            d = verdicts.get(c["symbol"], {"action": "REJECT", "reason": "no decision"})
            print(f"{now:%H:%M} {c['symbol']} {c['side']} qty={c['qty']}: {d['action']} - {d['reason']}")
            if d["action"] != "APPROVE":
                continue
            ok, why = can_open(len(broker.positions), broker.equity(prices), day_start_eq, rk)
            if not ok or n_trades >= rk["max_trades_per_day"]:
                print("   skipped:", why if not ok else "max trades per day"); continue
            res = broker.open(c["symbol"], c["side"], c["qty"], c["close"], c["stop"], c["target"])
            print("   ->", res)
            if str(res.get("status", "")).startswith(("sent", "filled")):
                n_trades += 1
