"""Brokers. PaperBroker simulates fills and persists state; KiteBroker places real orders via Zerodha.

Live orders require ALL of: mode=live in config, --live CLI flag, env LIVE_TRADING_CONFIRM=YES,
and an interactive 'yes' for every single order. Any one missing -> no real order is sent.
"""
from __future__ import annotations

import json
import os
from datetime import datetime


class PaperBroker:
    def __init__(self, cfg: dict, path: str = "state/paper.json"):
        self.path, self.slip, self.cost = path, cfg["slippage_pct"] / 100, cfg["cost_pct_per_side"] / 100
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            self.s = json.load(open(path))
        else:
            self.s = {"cash": float(cfg["starting_capital"]), "positions": {}, "log": [], "day_start_equity": None, "day": None}

    def _save(self):
        json.dump(self.s, open(self.path, "w"), indent=2)

    def positions(self) -> dict:
        return self.s["positions"]

    def equity(self, prices: dict[str, float]) -> float:
        return self.s["cash"] + sum(p["qty"] * prices.get(sym, p["entry"]) for sym, p in self.s["positions"].items())

    def start_day(self, equity: float) -> float:
        today = datetime.now().date().isoformat()
        if self.s["day"] != today:
            self.s["day"], self.s["day_start_equity"] = today, equity
            self._save()
        return self.s["day_start_equity"]

    def buy(self, sym: str, qty: int, price: float, stop: float, target: float) -> dict:
        fill = price * (1 + self.slip)
        self.s["cash"] -= qty * fill * (1 + self.cost)
        self.s["positions"][sym] = {"qty": qty, "entry": fill, "stop": stop, "target": target,
                                    "date": datetime.now().isoformat(timespec="seconds")}
        self._log("BUY", sym, qty, fill)
        return {"status": "filled (paper)", "price": fill}

    def sell(self, sym: str, price: float, reason: str) -> dict:
        p = self.s["positions"].pop(sym)
        fill = price * (1 - self.slip)
        self.s["cash"] += p["qty"] * fill * (1 - self.cost)
        self._log("SELL", sym, p["qty"], fill, reason)
        return {"status": "filled (paper)", "price": fill}

    def _log(self, side, sym, qty, price, note=""):
        self.s["log"].append({"t": datetime.now().isoformat(timespec="seconds"), "side": side, "symbol": sym,
                              "qty": qty, "price": round(price, 2), "note": note})
        self._save()


class KiteBroker(PaperBroker):
    """Zerodha Kite Connect. Positions/state are still tracked locally (stops are checked each run);
    entries and exits are sent as real CNC market orders after explicit confirmation.

    NOT included: automatic GTT/stop orders at the exchange. Place a stop-loss GTT yourself, or
    extend buy() to do so, because the agent only checks stops when it runs.
    """

    def __init__(self, cfg: dict, path: str = "state/live.json"):
        super().__init__(cfg, path)
        for var in ("KITE_API_KEY", "KITE_ACCESS_TOKEN"):
            if not os.environ.get(var):
                raise RuntimeError(f"{var} not set")
        if os.environ.get("LIVE_TRADING_CONFIRM") != "YES":
            raise RuntimeError("Set LIVE_TRADING_CONFIRM=YES to allow live orders")
        from kiteconnect import KiteConnect  # pip install kiteconnect

        self.kite = KiteConnect(api_key=os.environ["KITE_API_KEY"])
        self.kite.set_access_token(os.environ["KITE_ACCESS_TOKEN"])

    def _confirm(self, side, sym, qty, price) -> bool:
        ans = input(f"LIVE ORDER: {side} {qty} x {sym} @ ~{price:.2f} (CNC, market). Type 'yes' to send: ")
        return ans.strip().lower() == "yes"

    def _send(self, side, sym, qty):
        return self.kite.place_order(
            variety=self.kite.VARIETY_REGULAR, exchange=self.kite.EXCHANGE_NSE, tradingsymbol=sym,
            transaction_type=side, quantity=qty, product=self.kite.PRODUCT_CNC,
            order_type=self.kite.ORDER_TYPE_MARKET,
        )

    def buy(self, sym, qty, price, stop, target):
        if not self._confirm("BUY", sym, qty, price):
            return {"status": "skipped by user"}
        oid = self._send(self.kite.TRANSACTION_TYPE_BUY, sym, qty)
        self.s["positions"][sym] = {"qty": qty, "entry": price, "stop": stop, "target": target,
                                    "date": datetime.now().isoformat(timespec="seconds"), "order_id": oid}
        self._log("BUY", sym, qty, price, f"order {oid}")
        return {"status": "sent", "order_id": oid}

    def sell(self, sym, price, reason):
        qty = self.s["positions"][sym]["qty"]
        if not self._confirm("SELL", sym, qty, price):
            return {"status": "skipped by user"}
        oid = self._send(self.kite.TRANSACTION_TYPE_SELL, sym, qty)
        self.s["positions"].pop(sym)
        self._log("SELL", sym, qty, price, f"{reason}; order {oid}")
        return {"status": "sent", "order_id": oid}
