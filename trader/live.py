"""Live / paper intraday loop. One cycle per completed 5-minute candle during market hours (IST)."""
from __future__ import annotations

import json
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


class Session:
    """One trading day. step(now) processes the latest closed candle and is safe to call repeatedly
    (every minute or every 5 minutes): re-checking the same candle never repeats an order.
    Progress (symbols tried today, trade count, day-start equity) is saved so a restart resumes cleanly."""

    def __init__(self, cfg: dict, live: bool, dhan=None, path: str = "state/session.json"):
        self.cfg, self.live, self.path = cfg, live, path
        self.ip, self.rk, self.lv = cfg["intraday"], cfg["risk"], cfg["live"]
        if live and cfg.get("mode") != "live":
            raise SystemExit("Refusing: --live given but config mode is not 'live'.")
        self.dhan = dhan or make_dhan()
        self.syms = cfg["universe"]
        self.ids = resolve_security_ids(self.dhan, self.syms, cfg.get("dhan", {}).get("security_ids"))
        self.broker = DhanBroker(self.dhan, self.ids, self.rk, self.lv) if live else PaperIntradayBroker(self.rk)
        self.open_m, self.cut_m, self.sq_m = (_mins(self.ip[k]) for k in ("session_start", "entry_cutoff", "squareoff_time"))
        self.candle = self.ip["candle_minutes"]
        self.lines: list[str] = []
        today = now_ist().date().isoformat()
        st = {}
        try:
            with open(path) as f:
                st = json.load(f)
        except (OSError, ValueError):
            pass
        if st.get("day") != today or st.get("live") != live:
            st = {"day": today, "live": live, "traded": [], "n_trades": 0, "day_start_eq": None}
        self.st = st

    def say(self, msg: str) -> None:
        print(msg, flush=True)
        self.lines.append(msg)

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(self.st, f)

    def market_closed(self, now: datetime) -> bool:
        return now.weekday() >= 5 or now.hour * 60 + now.minute >= 15 * 60 + 30

    def step(self, now: datetime) -> str:
        """Returns 'done' (nothing more to do today), 'waiting' (opening range forming) or 'running'."""
        mins = now.hour * 60 + now.minute
        if self.market_closed(now):
            return "done"
        if mins < self.open_m + self.ip["or_minutes"]:
            return "waiting"
        ip, rk, broker = self.ip, self.rk, self.broker
        traded = set(self.st["traded"])

        bars = fetch_today(self.dhan, self.ids, self.syms, self.candle, now)
        prices = {s: float(df["close"].iloc[-1]) for s, df in bars.items()}
        broker.update(bars, prices)
        eq = broker.equity(prices)
        if self.st["day_start_eq"] is None:
            self.st["day_start_eq"] = eq
        tag = f"{now:%H:%M}"

        if mins >= self.sq_m or os.path.exists(SQUAREOFF_FILE):
            self.say(f"{tag} square-off")
            broker.square_off_all(prices)
            if os.path.exists(SQUAREOFF_FILE):
                os.remove(SQUAREOFF_FILE)
            self._save()
            if not broker.positions:
                self.say("Flat. Done for the day.")
                return "done"
            return "running"

        if mins > self.cut_m or os.path.exists(STOP_FILE):
            self.say(f"{tag} no new entries ({'paused' if os.path.exists(STOP_FILE) else 'after cutoff'}); "
                     f"open: {list(broker.positions)}")
            self._save()
            return "running"

        cands, ind = fresh_signals(bars, ip, now, set(broker.positions), traded)
        sized = []
        for c in cands:
            traded.add(c["symbol"])  # one attempt per symbol per day
            qty = position_size(eq, c["close"], c["stop"], rk, c["side"])
            if qty >= 1:
                sized.append({**c, "qty": qty})
        self.st["traded"] = sorted(traded)
        if not sized:
            self.say(f"{tag} no actionable signals; open: {list(broker.positions)}")
            self._save()
            return "running"

        pf = {"equity": round(eq), "positions": broker.positions}
        verdicts = {d["symbol"]: d for d in
                    review_candidates(sized, ind, pf, self.cfg["claude_review"], SYSTEM_INTRADAY)}
        for c in sized:
            d = verdicts.get(c["symbol"], {"action": "REJECT", "reason": "no decision"})
            self.say(f"{tag} {c['symbol']} {c['side']} qty={c['qty']}: {d['action']} - {d['reason']}")
            if d["action"] != "APPROVE":
                continue
            ok, why = can_open(len(broker.positions), broker.equity(prices), self.st["day_start_eq"], rk)
            if not ok or self.st["n_trades"] >= rk["max_trades_per_day"]:
                self.say("   skipped: " + (why if not ok else "max trades per day"))
                continue
            res = broker.open(c["symbol"], c["side"], c["qty"], c["close"], c["stop"], c["target"])
            self.say(f"   -> {res}")
            if str(res.get("status", "")).startswith(("sent", "filled")):
                self.st["n_trades"] += 1
        self._save()
        return "running"


def run_intraday(cfg: dict, live: bool) -> None:
    """Blocking loop for the background process / terminal: one step per completed candle."""
    if cfg.get("mode") == "live" and not live:
        print("[info] config mode is live but --live not passed; running in paper mode.")
    if os.path.exists(SQUAREOFF_FILE):
        os.remove(SQUAREOFF_FILE)
    if os.path.exists(STOP_FILE):
        print("[info] entries are paused (state/STOP exists) until it is removed.")
    sess = Session(cfg, live)
    base = cfg["risk"]["trading_capital"] if live else sess.broker.s["capital"]
    print(f"Mode: {'LIVE' if live else 'paper'} | symbols: {len(sess.syms)} | capital base: {base:,.0f}")
    while True:
        now = now_ist()
        if sess.market_closed(now):
            print("Market closed. Done.")
            return
        time.sleep(seconds_to_next_cycle(now, sess.candle, sess.lv["candle_delay_seconds"]))
        if sess.step(now_ist()) == "done":
            return
