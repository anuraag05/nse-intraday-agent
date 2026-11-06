"""Intraday brokers: PaperIntradayBroker (simulated) and DhanBroker (real orders via DhanHQ super orders).

Common interface
    open(sym, side, qty, price, stop, target) -> dict
    update(bars_by_symbol, prices)            # exits / sync with the broker
    square_off_all(prices)
    positions  (dict sym -> position)

DhanBroker puts entry, stop-loss and target on Dhan's side in ONE super order, so protection stays
active even if this program crashes. It is only used with --live, mode=live, LIVE_TRADING_CONFIRM=YES.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime

from . import approvals
from .intraday import check_exit


def _tick(x: float, tick: float = 0.05) -> float:
    return round(round(x / tick) * tick, 2)


class PaperIntradayBroker:
    def __init__(self, rk: dict, path: str = "state/intraday_paper.json"):
        self.path = path
        self.slip, self.cost = rk["slippage_pct"] / 100, rk["cost_pct_per_side"] / 100
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        today = datetime.now().date().isoformat()
        if os.path.exists(path):
            self.s = json.load(open(path))
        else:
            self.s = {"capital": float(rk["starting_capital"]), "day": today, "positions": {}, "log": []}
        if self.s["day"] != today:  # new day: carry capital, start flat
            self.s["day"], self.s["positions"] = today, {}
        self.day_start_capital = self.s["capital"]
        self._save()

    @property
    def positions(self) -> dict:
        return self.s["positions"]

    def _save(self):
        json.dump(self.s, open(self.path, "w"), indent=2, default=str)

    def equity(self, prices: dict) -> float:
        u = 0.0
        for sym, p in self.positions.items():
            c = prices.get(sym, p["entry"])
            u += (c - p["entry"]) * p["qty"] if p["side"] == "long" else (p["entry"] - c) * p["qty"]
        return self.s["capital"] + u

    def open(self, sym, side, qty, price, stop, target) -> dict:
        fill = price * (1 + self.slip) if side == "long" else price * (1 - self.slip)
        self.positions[sym] = {"side": side, "qty": qty, "entry": fill, "stop": stop, "target": target,
                               "since": datetime.now().isoformat(timespec="seconds")}
        self._log("OPEN", sym, side, qty, fill)
        return {"status": "filled (paper)", "price": round(fill, 2)}

    def _close(self, sym, raw, reason):
        p = self.positions.pop(sym)
        fill = raw * (1 - self.slip) if p["side"] == "long" else raw * (1 + self.slip)
        gross = (fill - p["entry"]) * p["qty"] if p["side"] == "long" else (p["entry"] - fill) * p["qty"]
        pnl = gross - p["qty"] * (p["entry"] + fill) * self.cost
        self.s["capital"] += pnl
        self._log("CLOSE", sym, p["side"], p["qty"], fill, f"{reason} pnl={pnl:.2f}")
        return pnl

    def update(self, bars: dict, prices: dict):
        """Check stops/targets against candles that completed after the position was opened."""
        for sym, p in list(self.positions.items()):
            df = bars.get(sym)
            if df is None or df.empty:
                continue
            since = p.get("checked_to") or p["since"]
            for ts, b in df[df.index > since].iterrows():
                hit = check_exit(p["side"], b["open"], b["high"], b["low"], p["stop"], p["target"])
                p["checked_to"] = str(ts)
                if hit:
                    self._close(sym, hit[0], hit[1])
                    break
        self._save()

    def square_off_all(self, prices: dict):
        for sym in list(self.positions):
            self._close(sym, prices.get(sym, self.positions[sym]["entry"]), "squareoff")
        self._save()

    def _log(self, kind, sym, side, qty, price, note=""):
        self.s["log"].append({"t": datetime.now().isoformat(timespec="seconds"), "kind": kind, "symbol": sym,
                              "side": side, "qty": qty, "price": round(price, 2), "note": note})
        self._save()


class DhanBroker:
    """Real orders through DhanHQ. Positions here are only the ones THIS agent opened; anything else in
    your Dhan account is never touched."""

    def __init__(self, dhan, ids: dict, rk: dict, live_cfg: dict, path: str = "state/intraday_live.json"):
        if os.environ.get("LIVE_TRADING_CONFIRM") != "YES":
            raise RuntimeError("Set LIVE_TRADING_CONFIRM=YES to allow live orders.")
        self.dhan, self.ids, self.path = dhan, ids, path
        self.confirm_each = live_cfg.get("confirm_each_order", True)
        self.buffer = live_cfg.get("entry_limit_buffer_pct", 0.15) / 100
        self.max_wait = live_cfg.get("cancel_unfilled_after_cycles", 2)
        self.capital = float(rk["trading_capital"])
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        today = datetime.now().date().isoformat()
        self.s = json.load(open(path)) if os.path.exists(path) else {"day": today, "positions": {}, "log": []}
        if self.s["day"] != today:
            self.s = {"day": today, "positions": {}, "log": []}
        self._save()

    @property
    def positions(self) -> dict:
        return self.s["positions"]

    def _save(self):
        json.dump(self.s, open(self.path, "w"), indent=2, default=str)

    def _log(self, msg):
        self.s["log"].append({"t": datetime.now().isoformat(timespec="seconds"), "msg": msg})
        self._save()

    def equity(self, prices: dict) -> float:
        return self.capital  # sizing base is the fixed trading_capital in config, not the whole account

    def _ok(self, text: str, auto_on_timeout: bool = False) -> bool:
        if not self.confirm_each:
            return True
        return approvals.request(text, default_on_timeout=auto_on_timeout)

    # -- entries --
    def open(self, sym, side, qty, price, stop, target) -> dict:
        limit = _tick(price * (1 + self.buffer) if side == "long" else price * (1 - self.buffer))
        stop, target = _tick(stop), _tick(target)
        txn = self.dhan.BUY if side == "long" else self.dhan.SELL
        if not self._ok(f"{txn} {qty} x {sym} LIMIT {limit} | SL {stop} | TGT {target} | INTRADAY"):
            return {"status": "skipped by user"}
        try:
            r = self.dhan.place_super_order(
                security_id=self.ids[sym], exchange_segment=self.dhan.NSE, transaction_type=txn,
                quantity=int(qty), order_type=self.dhan.LIMIT, product_type=self.dhan.INTRA,
                price=limit, targetPrice=target, stopLossPrice=stop,
            )
        except ValueError as e:  # the SDK validates leg prices
            return {"status": f"rejected before sending: {e}"}
        if r.get("status") != "success":
            self._log(f"ORDER FAILED {sym}: {r.get('remarks')}")
            return {"status": "failed", "detail": r.get("remarks")}
        data = r.get("data") or {}
        oid = data.get("orderId") if isinstance(data, dict) else None
        self.positions[sym] = {"side": side, "qty": int(qty), "entry": limit, "stop": stop, "target": target,
                               "order_id": oid, "state": "pending_entry", "waited": 0}
        self._log(f"OPENED {sym} {side} qty={qty} order_id={oid}")
        return {"status": "sent", "order_id": oid}

    # -- sync with the account --
    def _net_qty(self) -> dict[str, int] | None:
        r = self.dhan.get_positions()
        if r.get("status") != "success":
            return None
        out: dict[str, int] = {}
        for row in r.get("data") or []:
            if str(row.get("productType", "INTRADAY")).upper() not in ("INTRADAY", "INTRA"):
                continue
            sid = str(row.get("securityId"))
            out[sid] = out.get(sid, 0) + int(row.get("netQty", 0) or 0)
        return out

    def update(self, bars: dict, prices: dict):
        net = self._net_qty()
        if net is None:
            print("[dhan] could not read positions this cycle")
            return
        for sym, p in list(self.positions.items()):
            q = net.get(str(self.ids[sym]), 0)
            if p["state"] == "pending_entry":
                if q != 0:
                    p["state"] = "open"
                    self._log(f"FILLED {sym} net={q}")
                else:
                    p["waited"] += 1
                    if p["waited"] >= self.max_wait:
                        self._cancel(p)
                        self.positions.pop(sym)
                        self._log(f"CANCELLED unfilled entry {sym}")
            elif p["state"] == "open" and q == 0:
                self.positions.pop(sym)  # stop or target leg executed at the exchange
                self._log(f"CLOSED at broker {sym} (stop/target hit)")
        self._save()

    def _cancel(self, p):
        try:
            r = self.dhan.cancel_super_order(p["order_id"], "ENTRY_LEG")
            if r.get("status") != "success":
                print(f"[dhan] cancel warning: {r.get('remarks')}")
        except Exception as e:
            print(f"[dhan] cancel error: {e}")

    # -- end of day --
    def square_off_all(self, prices: dict):
        """Cancel our super orders, then flatten whatever net quantity remains in OUR symbols only."""
        for sym, p in list(self.positions.items()):
            self._cancel(p)
        time.sleep(2)
        net = self._net_qty() or {}
        for sym, p in list(self.positions.items()):
            q = net.get(str(self.ids[sym]), 0)
            if q != 0:
                txn = self.dhan.SELL if q > 0 else self.dhan.BUY
                if self._ok(f"SQUARE OFF {txn} {abs(q)} x {sym} MARKET INTRADAY", auto_on_timeout=True):
                    r = self.dhan.place_order(
                        security_id=self.ids[sym], exchange_segment=self.dhan.NSE, transaction_type=txn,
                        quantity=abs(q), order_type=self.dhan.MARKET, product_type=self.dhan.INTRA, price=0,
                    )
                    self._log(f"SQUAREOFF {sym} qty={q} -> {r.get('status')}")
            self.positions.pop(sym, None)
        time.sleep(2)
        left = {s: q for s, q in (self._net_qty() or {}).items() if q}
        mine = {s for s in self.ids if str(self.ids[s]) in left}
        if mine:
            print(f"[WARNING] open positions remain in {sorted(mine)} - check your Dhan app now.")
        self._save()
