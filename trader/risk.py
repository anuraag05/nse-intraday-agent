"""Position sizing and portfolio-level risk checks. These are hard rules the AI cannot override."""
from __future__ import annotations


def position_size(equity: float, entry: float, stop: float, r: dict, side: str = "long") -> int:
    """Shares so that a stop-out loses ~risk_per_trade_pct of equity, capped by max_position_pct.
    side='short' expects stop above entry."""
    risk_per_share = (entry - stop) if side == "long" else (stop - entry)
    if risk_per_share <= 0 or entry <= 0:
        return 0
    by_risk = (equity * r["risk_per_trade_pct"] / 100) / risk_per_share
    by_cap = (equity * r["max_position_pct"] / 100) / entry
    return max(int(min(by_risk, by_cap)), 0)


def can_open(n_open: int, equity: float, day_start_equity: float, r: dict) -> tuple[bool, str]:
    if n_open >= r["max_open_positions"]:
        return False, "max open positions reached"
    if day_start_equity > 0:
        dd = (day_start_equity - equity) / day_start_equity * 100
        if dd >= r["daily_loss_limit_pct"]:
            return False, f"daily loss limit hit ({dd:.2f}%)"
    return True, "ok"
