"""NSE Intraday Agent - point-and-click dashboard (no command line needed).
Launch with the Start script for your system (see README), or: streamlit run app.py
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from trader import approvals, runner, settings  # noqa: E402
from trader.dhan_client import fetch_candles, load_env, make_dhan, resolve_security_ids, save_env, token_expiry  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
load_env()
st.set_page_config(page_title="NSE Intraday Agent", page_icon="📈", layout="wide")


def cfg() -> dict:
    return settings.load_intraday_cfg()


def flag(name: str) -> Path:
    return ROOT / "state" / name


def hhmm_ok(x: str) -> bool:
    return bool(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", x.strip()))


def token_state() -> tuple[str, str]:
    """(level, message) about the saved access token."""
    tok, cid = os.environ.get("DHAN_ACCESS_TOKEN"), os.environ.get("DHAN_CLIENT_ID")
    if not cid or not tok:
        return "missing", "No Dhan credentials saved yet - go to the Setup tab."
    exp = token_expiry(tok)
    if exp is None:
        return "unknown", "Token saved (expiry could not be read)."
    left = exp - datetime.now(timezone.utc).replace(tzinfo=None)
    local = (exp + timedelta(hours=5, minutes=30)).strftime("%d %b %H:%M IST")
    if left.total_seconds() <= 0:
        return "expired", f"Token expired at {local}. Generate a new one in Dhan and paste it in Setup."
    hrs = left.total_seconds() / 3600
    if hrs < 2:
        return "soon", f"Token expires in {hrs:.1f} h ({local}). Replace it before running the agent."
    return "ok", f"Token valid for another {hrs:.0f} h (until {local})."


# ------------------------------------------------------------------ header
st.title("📈 NSE Intraday Agent")
agent = runner.status()
lvl, msg = token_state()
c1, c2 = st.columns([1, 2])
with c1:
    if agent["running"]:
        st.success(f"Agent running ({'LIVE' if agent.get('live') else 'paper'})")
    else:
        st.info("Agent stopped")
with c2:
    {"ok": st.success, "soon": st.warning, "expired": st.error, "missing": st.warning, "unknown": st.info}[lvl](msg)

tab_setup, tab_settings, tab_bt, tab_run, tab_hist = st.tabs(
    ["1 · Setup", "2 · Settings", "3 · Backtest", "4 · Run", "5 · History"]
)

# ------------------------------------------------------------------ setup
with tab_setup:
    st.subheader("Dhan credentials")
    st.caption("Saved only in a private file (.env) on this computer. They are never uploaded or shown again. "
               "The access token expires every 24 hours - come back here each morning and paste a new one.")
    with st.form("creds"):
        cid = st.text_input("Dhan client ID", value=os.environ.get("DHAN_CLIENT_ID", ""), key="cid")
        tok = st.text_input("Dhan access token", value="", type="password", key="tok",
                            placeholder="(saved)" if os.environ.get("DHAN_ACCESS_TOKEN") else "paste token here")
        akey = st.text_input("Anthropic API key (optional - enables the Claude trade reviewer)", value="",
                             type="password", key="akey", placeholder="(saved)" if os.environ.get("ANTHROPIC_API_KEY") else "")
        if st.form_submit_button("Save credentials", type="primary"):
            vals = {}
            if cid.strip():
                vals["DHAN_CLIENT_ID"] = cid.strip()
            if tok.strip():
                vals["DHAN_ACCESS_TOKEN"] = tok.strip()
            if akey.strip():
                vals["ANTHROPIC_API_KEY"] = akey.strip()
            if vals:
                save_env(vals)
                st.success("Saved.")
                st.rerun()
            else:
                st.warning("Nothing to save.")

    st.subheader("Test the connection")
    sym = st.text_input("Symbol to test", value="RELIANCE", key="test_sym").strip().upper()
    if st.button("Test Dhan connection", key="test_btn"):
        try:
            with st.spinner("Contacting Dhan..."):
                c = cfg()
                dhan = make_dhan()
                ids = resolve_security_ids(dhan, [sym], c.get("dhan", {}).get("security_ids"))
                now = datetime.now(IST).replace(tzinfo=None)
                df = fetch_candles(dhan, ids[sym], now - timedelta(days=4), now, c["intraday"]["candle_minutes"])
            if df.empty:
                st.warning(f"Connected, but no candles came back for {sym} (market holiday or no Data API access?).")
            else:
                st.success(f"Connected. {sym} (security id {ids[sym]}): {len(df)} five-minute candles received.")
                st.dataframe(df.tail(10), use_container_width=True)
                st.line_chart(df["close"].tail(150))
                st.caption("Check that the latest candle times and prices match your charting app. "
                           "Times should be the candle START in IST.")
        except Exception as e:
            st.error(f"Could not fetch data: {e}")
            st.caption("Common causes: token expired or mistyped, Data API not enabled on your Dhan account, "
                       "or your network blocks Dhan.")

# ------------------------------------------------------------------ settings
with tab_settings:
    c = cfg()
    ip, rk, lv, cr = c["intraday"], c["risk"], c["live"], c["claude_review"]
    st.subheader("Risk limits")
    with st.form("settings"):
        a, b, d = st.columns(3)
        trading_capital = a.number_input("Live trading capital (₹)", 10000, 100_000_000, int(rk["trading_capital"]), 10000,
                                         help="The agent sizes trades from this amount, not your whole account.")
        start_cap = b.number_input("Paper / backtest capital (₹)", 10000, 100_000_000, int(rk["starting_capital"]), 10000)
        risk_pct = d.number_input("Risk per trade (% of capital)", 0.05, 5.0, float(rk["risk_per_trade_pct"]), 0.05)
        a, b, d = st.columns(3)
        max_pos = a.number_input("Max open positions", 1, 20, int(rk["max_open_positions"]))
        max_trades = b.number_input("Max trades per day", 1, 50, int(rk["max_trades_per_day"]))
        loss_lim = d.number_input("Stop new trades when day is down (%)", 0.1, 10.0, float(rk["daily_loss_limit_pct"]), 0.1)

        st.subheader("Strategy (opening-range breakout)")
        a, b, d = st.columns(3)
        or_min = a.selectbox("Opening range (minutes)", [10, 15, 30, 45], index=[10, 15, 30, 45].index(ip["or_minutes"])
                             if ip["or_minutes"] in (10, 15, 30, 45) else 1)
        cutoff = b.text_input("No new entries after (HH:MM)", ip["entry_cutoff"])
        sq = d.text_input("Square off everything at (HH:MM)", ip["squareoff_time"])
        a, b, d = st.columns(3)
        rr = a.number_input("Reward : risk", 0.5, 5.0, float(ip["reward_risk"]), 0.1)
        atr_m = b.number_input("Stop distance (× ATR)", 0.5, 5.0, float(ip["atr_stop_mult"]), 0.1)
        vol_m = d.number_input("Breakout volume (× average)", 1.0, 5.0, float(ip["volume_mult"]), 0.1)
        shorts = st.checkbox("Allow short selling", value=bool(ip["allow_short"]))

        st.subheader("Stocks to watch")
        uni = st.text_area("NSE symbols (separate with commas or new lines)", ", ".join(c["universe"]), height=90)

        st.subheader("Safety")
        confirm = st.checkbox("Ask me to approve every live order (recommended)", value=bool(lv["confirm_each_order"]))
        claude_on = st.checkbox("Let Claude review each trade idea before it is taken (needs Anthropic key)",
                                value=bool(cr["enabled"]))
        saved = st.form_submit_button("Save settings", type="primary")
    if saved:
        errs = []
        if not (hhmm_ok(cutoff) and hhmm_ok(sq)):
            errs.append("Times must look like 14:30.")
        elif cutoff >= sq:
            errs.append("The entry cutoff must be earlier than the square-off time.")
        syms = list(dict.fromkeys(s.strip().upper() for s in re.split(r"[,\s]+", uni) if s.strip()))
        if not syms:
            errs.append("Add at least one symbol.")
        if errs:
            for e in errs:
                st.error(e)
        else:
            settings.save_user_overrides({
                "universe": syms,
                "risk": {"trading_capital": int(trading_capital), "starting_capital": int(start_cap),
                         "risk_per_trade_pct": float(risk_pct), "max_open_positions": int(max_pos),
                         "max_trades_per_day": int(max_trades), "daily_loss_limit_pct": float(loss_lim)},
                "intraday": {"or_minutes": int(or_min), "entry_cutoff": cutoff.strip(), "squareoff_time": sq.strip(),
                             "reward_risk": float(rr), "atr_stop_mult": float(atr_m), "volume_mult": float(vol_m),
                             "allow_short": bool(shorts)},
                "live": {"confirm_each_order": bool(confirm)},
                "claude_review": {"enabled": bool(claude_on)},
            })
            st.success("Settings saved.")
    if st.button("Reset all settings to defaults", key="reset_btn"):
        settings.reset_user_overrides()
        st.rerun()

# ------------------------------------------------------------------ backtest
with tab_bt:
    st.subheader("Backtest")
    st.caption("Replays the strategy on past 5-minute candles with trading costs and slippage. "
               "A short test is a small sample - treat results as a sanity check, not a promise.")
    a, b = st.columns([2, 1])
    source = a.radio("Data", ["Dhan (real market data)", "Demo (random data - only to try the app)"], key="bt_source")
    days = b.slider("Calendar days of history", 5, 90, 30, key="bt_days")
    if st.button("Run backtest", type="primary", key="bt_run"):
        from trader.datasets import load_intraday
        from trader.intraday import backtest_intraday

        c = cfg()
        bar = st.progress(0.0, text="Loading candles...")
        try:
            data = load_intraday(c, days, source="synthetic" if source.startswith("Demo") else "dhan",
                                 progress=lambda i, n, s: bar.progress(i / n, text=f"Loaded {s} ({i}/{n})"))
            bar.empty()
            if not data:
                st.error("No data loaded. Check the Setup tab.")
            else:
                res = backtest_intraday(data, c)
                st.session_state["bt"] = {"res": res, "demo": source.startswith("Demo"), "n": len(data)}
        except Exception as e:
            bar.empty()
            st.error(f"Backtest failed: {e}")
    bt = st.session_state.get("bt")
    if bt:
        m, t, eq = bt["res"]["metrics"], bt["res"]["trades"], bt["res"]["equity"]
        if bt["demo"]:
            st.warning("Demo data is random, so these numbers say nothing about the strategy.")
        cols = st.columns(5)
        cols[0].metric("Total return", f"{m.get('total_return_pct', 0)}%")
        cols[1].metric("Max drawdown", f"{m.get('max_drawdown_pct', 0)}%")
        cols[2].metric("Trades", m.get("trades", 0))
        cols[3].metric("Win rate", f"{m.get('win_rate_pct', 0)}%")
        cols[4].metric("Profit factor", m.get("profit_factor") if m.get("profit_factor") is not None else "-")
        if m.get("trades", 0) < 30:
            st.info("Fewer than 30 trades: too few to judge anything.")
        if not eq.empty:
            st.line_chart(eq.rename("Capital (₹)"))
        if not t.empty:
            x1, x2 = st.columns(2)
            x1.caption("Result by exit type")
            x1.dataframe(t.groupby("reason")["pnl"].agg(["count", "sum"]).round(0), use_container_width=True)
            x2.caption("Result by side")
            x2.dataframe(t.groupby("side")["pnl"].agg(["count", "sum"]).round(0), use_container_width=True)
            st.dataframe(t, use_container_width=True, height=300)
            st.download_button("Download trades (CSV)", t.to_csv(index=False), "backtest_trades.csv", key="bt_dl")

# ------------------------------------------------------------------ run
with tab_run:
    st.subheader("Run the agent")
    c = cfg()
    running = runner.status()
    if not running["running"]:
        mode = st.radio("Mode", ["Paper trading (simulated, no real orders)", "LIVE (real orders with real money)"],
                        key="run_mode")
        live = mode.startswith("LIVE")
        ready = lvl in ("ok", "soon")
        if not ready:
            st.warning("Add a valid, unexpired Dhan token in Setup first (the agent needs it for market data).")
        go = True
        if live:
            st.error("Live mode sends real orders to your Dhan account. Losses are real. Paper-trade for several weeks "
                     "first, and start with a small trading capital.")
            if not c["live"]["confirm_each_order"]:
                st.warning("Per-order approval is OFF: orders will be placed automatically.")
            ok1 = st.checkbox("I understand this uses real money and I am responsible for every trade", key="live_ack")
            ok2 = st.text_input('Type GO LIVE to enable', key="live_type").strip() == "GO LIVE"
            go = ok1 and ok2
        now = datetime.now(IST)
        if now.weekday() >= 5 or not (8 * 60 <= now.hour * 60 + now.minute < 15 * 60 + 25):
            st.caption("Note: the market is closed right now. The agent waits for the session; start it before 09:15 IST.")
        if st.button("Start agent", type="primary", disabled=not (ready and go), key="start_btn"):
            try:
                runner.start(live)
                st.rerun()
            except Exception as e:
                st.error(str(e))
    else:
        st.write(f"Running since {datetime.fromtimestamp(running['started'], IST):%H:%M:%S IST} "
                 f"in **{'LIVE' if running.get('live') else 'paper'}** mode.")

    @st.fragment(run_every=3)
    def live_panel():
        s = runner.status()
        if not s["running"]:
            tail = runner.tail_log()
            if tail:
                st.caption("Last run's log")
                st.code(tail, language=None)
            return
        pend = approvals.pending()
        if pend:
            st.warning(f"{len(pend)} order(s) waiting for your approval")
        for p in pend:
            left = int(p["created"] + p["timeout"] - datetime.now().timestamp())
            st.markdown(f"**{p['text']}**  \n_expires in {max(left, 0)} s_")
            a, b, _ = st.columns([1, 1, 4])
            if a.button("Approve", key=f"ap_{p['id']}", type="primary"):
                approvals.decide(p["id"], True)
            if b.button("Reject", key=f"rj_{p['id']}"):
                approvals.decide(p["id"], False)

        st.markdown("**Open positions**")
        pf = ROOT / "state" / ("intraday_live.json" if s.get("live") else "intraday_paper.json")
        try:
            pos = json.loads(pf.read_text()).get("positions", {})
        except (OSError, ValueError):
            pos = {}
        st.dataframe(pd.DataFrame(pos).T if pos else pd.DataFrame({"": ["none"]}), use_container_width=True)

        paused = flag("STOP").exists()
        a, b, d = st.columns(3)
        if a.button("▶ Resume new entries" if paused else "⏸ Pause new entries", key="pause_btn"):
            if paused:
                flag("STOP").unlink(missing_ok=True)
            else:
                flag("STOP").parent.mkdir(exist_ok=True)
                flag("STOP").write_text("")
            st.rerun(scope="fragment")
        if b.button("Square off all now", key="sq_btn"):
            flag("SQUAREOFF").parent.mkdir(exist_ok=True)
            flag("SQUAREOFF").write_text("")
            st.toast("Square-off requested - it happens at the next 5-minute candle.")
        if d.button("Stop agent", key="stop_btn"):
            runner.stop()
            st.rerun()
        if paused:
            st.info("New entries are paused. Existing positions keep their stop-loss and target.")
        if s.get("live"):
            st.caption("Stopping the agent does not close live positions; their stop-loss/target stay active at Dhan "
                       "until the broker's own intraday auto square-off. Check your Dhan app.")
        st.markdown("**Agent log**")
        st.code(runner.tail_log() or "(waiting for output...)", language=None)

    live_panel()

# ------------------------------------------------------------------ history
with tab_hist:
    st.subheader("History")
    for title, fn in [("Paper trading", "intraday_paper.json"), ("Live trading", "intraday_live.json")]:
        p = ROOT / "state" / fn
        if not p.exists():
            continue
        try:
            d = json.loads(p.read_text())
        except ValueError:
            continue
        st.markdown(f"**{title}**" + (f" - capital ₹{d['capital']:,.0f}" if "capital" in d else ""))
        if d.get("log"):
            st.dataframe(pd.DataFrame(d["log"]).iloc[::-1], use_container_width=True, height=260)
        else:
            st.caption("No activity yet.")
    rec = approvals.recent(15)
    if rec:
        st.markdown("**Recent order approvals**")
        st.dataframe(pd.DataFrame([{"time": datetime.fromtimestamp(r["created"], IST).strftime("%H:%M:%S"),
                                    "order": r["text"], "result": r["status"]} for r in rec]), use_container_width=True)
