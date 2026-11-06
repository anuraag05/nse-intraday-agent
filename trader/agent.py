"""Claude as the reviewing analyst.

The rules (strategy.py) find candidates and risk.py sizes them. Claude can only APPROVE or REJECT
candidates (never change size, add symbols, or bypass risk limits). It can call tools to look at
recent price action and the portfolio before deciding.
"""
from __future__ import annotations

import json
import os

SYSTEM = """You are a cautious risk-aware reviewer for an end-of-day NSE (India) equity trading system.
A rules-based scanner has proposed long-entry candidates. For each one, decide APPROVE or REJECT.
Reject setups that look like exhaustion (extended far above the moving averages, a huge one-day spike
into resistance), poor liquidity, over-concentration with existing holdings (same sector/theme), or
anything suspicious in the recent bars. You cannot see live news; do not claim to. When unsure, REJECT.
Use the tools if useful, then call submit_decisions exactly once with a decision for every candidate.
Keep each reason to one sentence."""

SYSTEM_INTRADAY = """You are a cautious risk-aware reviewer for an intraday NSE (India) equity trading system.
A rules-based opening-range-breakout scanner proposed these candidates (side is long or short; positions
are squared off by ~15:10 IST). For each one decide APPROVE or REJECT. Reject setups that look like a
false breakout or exhaustion: a huge spike candle far from VWAP, a breakout straight into the prior
day's high/low you can see in the bars, choppy overlapping candles, thin volume, or too many correlated
names already open in the same direction. You cannot see live news; do not claim to. Decide quickly;
at most one tool call per candidate. When unsure, REJECT. Call submit_decisions exactly once.
Keep each reason to one sentence."""

TOOLS = [
    {
        "name": "get_recent_bars",
        "description": "Last N daily OHLCV bars with EMA20/EMA50/ATR for a symbol.",
        "input_schema": {"type": "object", "properties": {"symbol": {"type": "string"}, "n": {"type": "integer"}},
                         "required": ["symbol"]},
    },
    {
        "name": "get_portfolio",
        "description": "Current open positions, cash and equity.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "submit_decisions",
        "description": "Submit final decisions for all candidates.",
        "input_schema": {
            "type": "object",
            "properties": {
                "decisions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "symbol": {"type": "string"},
                            "action": {"type": "string", "enum": ["APPROVE", "REJECT"]},
                            "reason": {"type": "string"},
                        },
                        "required": ["symbol", "action", "reason"],
                    },
                }
            },
            "required": ["decisions"],
        },
    },
]


def review_candidates(candidates: list[dict], indicators: dict, portfolio: dict, rcfg: dict,
                      system: str = SYSTEM) -> list[dict]:
    """Returns [{symbol, action, reason}]. Falls back to rule-only approval if Claude is unavailable."""
    if not candidates:
        return []
    if not rcfg.get("enabled") or not os.environ.get("ANTHROPIC_API_KEY"):
        return [{"symbol": c["symbol"], "action": "APPROVE", "reason": "rules only (Claude review off)"} for c in candidates]

    import anthropic  # pip install anthropic

    client = anthropic.Anthropic()
    from .research import research_notes_for_claude

    notes = research_notes_for_claude()
    intro = f"Findings from our own backtest research (use as context, they can be wrong):\n{notes}\n\n" if notes else ""
    msgs = [{"role": "user", "content": intro + "Candidates:\n" + json.dumps(candidates, indent=2)}]
    known = {c["symbol"] for c in candidates}

    for _ in range(rcfg.get("max_turns", 8)):
        resp = client.messages.create(model=rcfg["model"], max_tokens=2000, system=system, tools=TOOLS, messages=msgs)
        msgs.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            if block.name == "submit_decisions":
                decided = {d["symbol"]: d for d in block.input.get("decisions", []) if d.get("symbol") in known}
                # anything Claude skipped is rejected by default
                return [decided.get(c["symbol"], {"symbol": c["symbol"], "action": "REJECT", "reason": "no decision given"})
                        for c in candidates]
            if block.name == "get_recent_bars":
                out = _bars(indicators, block.input.get("symbol", ""), int(block.input.get("n", 20)))
            elif block.name == "get_portfolio":
                out = json.dumps(portfolio)
            else:
                out = "unknown tool"
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})
        if not results:
            msgs.append({"role": "user", "content": "Please call submit_decisions now."})
        else:
            msgs.append({"role": "user", "content": results})
    return [{"symbol": c["symbol"], "action": "REJECT", "reason": "review did not finish"} for c in candidates]


def _bars(indicators: dict, symbol: str, n: int) -> str:
    df = indicators.get(symbol)
    if df is None:
        return "unknown symbol"
    wanted = ["open", "high", "low", "close", "volume", "ema_fast", "ema_slow", "vwap", "atr"]
    cols = [c for c in wanted if c in df.columns]
    return df[cols].tail(min(max(n, 1), 60)).round(2).to_csv()
