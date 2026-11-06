#!/usr/bin/env python3
"""NSE trading agent CLI.

  python main.py backtest            # test the strategy on history
  python main.py scan                # show today's candidate signals (no orders)
  python main.py run                 # paper-trade one end-of-day cycle
  python main.py run --live          # real orders (needs Kite creds + LIVE_TRADING_CONFIRM=YES + per-order 'yes')
  python main.py status              # show paper portfolio

Intraday (Dhan, 5-minute ORB; uses config_intraday.yaml):
  python main.py icheck [SYMBOL]     # verify Dhan credentials/data: print latest candles
  python main.py ibacktest           # backtest the intraday strategy
  python main.py iscan               # signals on the latest closed candle (no orders)
  python main.py irun [--live]       # run the live loop for today's session (paper unless --live)
"""
from __future__ import annotations

import argparse
import json
import sys

import yaml

from trader.agent import review_candidates
from trader.backtest import backtest
from trader.broker import KiteBroker, PaperBroker
from trader.data import load_prices
from trader.risk import can_open, position_size
from trader.strategy import add_indicators, latest_signals, should_exit


def load_cfg(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def cmd_backtest(cfg, args):
    data = load_prices(cfg["universe"], cfg["data"])
    if not data:
        sys.exit("No data loaded.")
    res = backtest(data, cfg)
    print(json.dumps(res["metrics"], indent=2))
    if not res["trades"].empty:
        res["trades"].to_csv("backtest_trades.csv", index=False)
        res["equity"].to_csv("backtest_equity.csv", header=["equity"])
        print("Saved backtest_trades.csv and backtest_equity.csv")
    print("\nReminder: past results do not predict future returns. Test out-of-sample before risking money.")


def cmd_scan(cfg, args):
    data = load_prices(cfg["universe"], cfg["data"])
    sigs = latest_signals(data, cfg["strategy"])
    print(json.dumps(sigs, indent=2) if sigs else "No signals on the latest bar.")


def cmd_status(cfg, args):
    b = PaperBroker(cfg["risk"])
    print(json.dumps({"cash": round(b.s["cash"], 2), "positions": b.positions()}, indent=2))


def cmd_run(cfg, args):
    live = args.live
    if live and cfg.get("mode") != "live":
        sys.exit("Refusing: --live given but config mode is not 'live'.")
    if cfg.get("mode") == "live" and not live:
        print("[info] config mode is live but --live not passed; running in paper mode.")
    rk, sp = cfg["risk"], cfg["strategy"]
    broker = KiteBroker(rk) if live else PaperBroker(rk)

    data = load_prices(cfg["universe"], cfg["data"])
    ind = {s: add_indicators(df, sp) for s, df in data.items()}
    prices = {s: float(df["close"].iloc[-1]) for s, df in ind.items()}
    equity = broker.equity(prices)
    day_start = broker.start_day(equity)
    print(f"Equity: {equity:,.0f}  Open positions: {len(broker.positions())}")

    # 1) manage exits (rules decide; Claude is not involved in exits)
    for sym, pos in list(broker.positions().items()):
        if sym not in ind:
            print(f"[warn] no data for held {sym}; check it manually")
            continue
        reason = should_exit(ind[sym].iloc[-1], pos)
        if reason:
            print(f"EXIT {sym} ({reason}) -> {broker.sell(sym, prices[sym], reason)}")

    # 2) find, size and review entries
    held = set(broker.positions())
    cands = []
    for sig in latest_signals(data, sp):
        if sig["symbol"] in held:
            continue
        qty = position_size(equity, sig["close"], sig["stop"], rk)
        if qty >= 1:
            cands.append({**sig, "qty": qty, "capital": round(qty * sig["close"])})
    print(f"{len(cands)} candidate(s) after sizing")

    portfolio = {"cash": round(broker.s["cash"]), "equity": round(equity), "positions": broker.positions()}
    decisions = review_candidates(cands, ind, portfolio, cfg["claude_review"])
    verdict = {d["symbol"]: d for d in decisions}

    # 3) execute approved entries, with hard risk checks
    for c in cands:
        d = verdict.get(c["symbol"], {"action": "REJECT", "reason": "no decision"})
        print(f"{c['symbol']}: {d['action']} - {d['reason']}")
        if d["action"] != "APPROVE":
            continue
        equity = broker.equity(prices)
        ok, why = can_open(len(broker.positions()), equity, day_start, rk)
        if not ok:
            print(f"  skipped: {why}")
            continue
        cost = c["qty"] * c["close"] * (1 + rk["cost_pct_per_side"] / 100)
        if cost > broker.s["cash"]:
            print("  skipped: not enough cash")
            continue
        print("  ->", broker.buy(c["symbol"], c["qty"], c["close"], c["stop"], c["target"]))


def _icfg(args):
    if args.config != "config.yaml":  # explicit config file given
        return load_cfg(args.config)
    from trader.settings import load_intraday_cfg

    return load_intraday_cfg()


def _intraday_data(cfg, days=None):
    from trader.datasets import load_intraday

    return load_intraday(cfg, days)


def cmd_icheck(cfg, args):
    from datetime import datetime, timedelta

    from trader.dhan_client import fetch_candles, make_dhan, resolve_security_ids

    sym = args.symbol or cfg["universe"][0]
    dhan = make_dhan()
    ids = resolve_security_ids(dhan, [sym], cfg.get("dhan", {}).get("security_ids"))
    now = datetime.now()
    df = fetch_candles(dhan, ids[sym], now - timedelta(days=4), now, cfg["intraday"]["candle_minutes"])
    print(f"{sym} security_id={ids[sym]}  candles={len(df)}")
    print(df.tail(10).to_string())
    print("\nCompare the last candles' times/prices with your charting app. Candle time should be the candle START (IST).")


def cmd_ibacktest(cfg, args):
    from trader.intraday import backtest_intraday

    data = _intraday_data(cfg)
    if not data:
        sys.exit("No data loaded.")
    res = backtest_intraday(data, cfg)
    print(json.dumps(res["metrics"], indent=2))
    if not res["trades"].empty:
        t = res["trades"]
        print("\nBy exit reason:\n", t.groupby("reason")["pnl"].agg(["count", "sum"]).round(0).to_string())
        print("\nBy side:\n", t.groupby("side")["pnl"].agg(["count", "sum"]).round(0).to_string())
        t.to_csv("ibacktest_trades.csv", index=False)
        print("\nSaved ibacktest_trades.csv")
    print("\nA 30-day sample is small. Past results do not predict future returns.")


def cmd_iscan(cfg, args):
    from trader.dhan_client import make_dhan, resolve_security_ids
    from trader.live import fetch_today, now_ist

    dhan = make_dhan()
    ids = resolve_security_ids(dhan, cfg["universe"], cfg.get("dhan", {}).get("security_ids"))
    now = now_ist()
    bars = fetch_today(dhan, ids, cfg["universe"], cfg["intraday"]["candle_minutes"], now)
    from trader.intraday import add_indicators, signal_from_row

    found = []
    for s, df in bars.items():
        sig = signal_from_row(add_indicators(df, cfg["intraday"]).iloc[-1], cfg["intraday"])
        if sig:
            found.append({"symbol": s, "candle": str(df.index[-1]), **{k: round(v, 2) if isinstance(v, float) else v
                                                                      for k, v in sig.items()}})
    print(json.dumps(found, indent=2) if found else "No signals on the latest closed candles.")


def cmd_research(cfg, args):
    from trader import research

    data = _intraday_data(cfg, 180)
    if not data:
        sys.exit("No data loaded.")
    res = research.run(data, cfg, progress=lambda i, n: print(f"\rsearching settings {i}/{n}", end=""))
    print(f"\n\nVERDICT: {res['verdict']}\n{res['message']}")
    print("Out-of-sample:", {k: round(v, 3) for k, v in res["oos_stats"].items()})
    for name, tab in res.get("insights", {}).items():
        print(f"\n== {name} ==\n{tab.to_string()}")
    print("\nNotes:", *res.get("notes", []), sep="\n - ")
    research.save_learned(res)
    if research.applicable(res):
        print("\nSuggested settings:", res["best_params"], "\n(apply them in the dashboard's Learn tab)")
    else:
        print("\nNo settings are recommended.")


def cmd_irun(cfg, args):
    from trader.live import run_intraday

    run_intraday(cfg, args.live)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["backtest", "scan", "run", "status", "icheck", "ibacktest", "iscan", "irun", "research"])
    ap.add_argument("symbol", nargs="?", help="symbol for icheck")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--live", action="store_true", help="send REAL orders (see README)")
    args = ap.parse_args()
    if args.command.startswith("i") or args.command == "research":
        cfg = _icfg(args)
        {"icheck": cmd_icheck, "ibacktest": cmd_ibacktest, "iscan": cmd_iscan, "irun": cmd_irun,
         "research": cmd_research}[args.command](cfg, args)
    else:
        cfg = load_cfg(args.config)
        {"backtest": cmd_backtest, "scan": cmd_scan, "run": cmd_run, "status": cmd_status}[args.command](cfg, args)


if __name__ == "__main__":
    main()
