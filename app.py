"""NSE Intraday Agent - point-and-click dashboard (no command line needed).
Launch with the Start script for your system (see README), or: streamlit run app.py
"""
from __future__ import annotations

import hmac
import json
import os
import re
import sys
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from trader import approvals, runner, settings  # noqa: E402
from trader import live as live_mod  # noqa: E402
from trader.dhan_client import fetch_candles, load_env, make_dhan, resolve_security_ids, save_env, token_expiry  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
load_env()
st.set_page_config(page_title="NSE Intraday Agent", page_icon="📈", layout="wide")


def _secret(key: str):
    try:
        return st.secrets.get(key)
    except Exception:  # no secrets file / not configured
        return None


for _k in ("DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN", "ANTHROPIC_API_KEY"):
    if _secret(_k):
        os.environ.setdefault(_k, str(_secret(_k)))
# Hosted (e.g. Streamlit Community Cloud): no background process, no live orders (Dhan needs a registered static IP)
HOSTED = os.environ.get("HOSTED") == "1" or bool(_secret("HOSTED")) or str(ROOT).startswith("/mount/src")

_pw = _secret("APP_PASSWORD") or os.environ.get("APP_PASSWORD")
if _pw and not st.session_state.get("authed"):
    st.title("📈 NSE Intraday Agent")
    entered = st.text_input("Password", type="password", key="pw")
    if entered and hmac.compare_digest(entered.encode(), str(_pw).encode()):
        st.session_state["authed"] = True
        st.rerun()
    elif entered:
        st.error("Wrong password.")
    st.stop()


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
    if HOSTED and st.session_state.get("tick_on"):
        st.success("Paper session running (keep this page open)")
    elif agent["running"]:
        st.success(f"Agent running ({'LIVE' if agent.get('live') else 'paper'})")
    else:
        st.info("Agent stopped")
with c2:
    {"ok": st.success, "soon": st.warning, "expired": st.error, "missing": st.warning, "unknown": st.info}[lvl](msg)

if HOSTED and not _pw:
    st.warning("Anyone with this page's link can open it. Add APP_PASSWORD in the app's Secrets to lock it.")

tab_setup, tab_settings, tab_bt, tab_learn, tab_run, tab_hist = st.tabs(
    ["1 · Setup", "2 · Settings", "3 · Backtest", "4 · Learn", "5 · Run", "6 · History"]
)

# ------------------------------------------------------------------ setup
with tab_setup:
    st.subheader("Dhan credentials")
    if HOSTED:
        st.caption("Best: put DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in this app's Secrets (App menu > Settings > Secrets). "
                   "Anything you paste below only lasts until the app restarts. The token expires every 24 hours, "
                   "so update it each morning.")
    else:
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

# ------------------------------------------------------------------ learn
with tab_learn:
    from trader import research

    st.subheader("Learn what works")
    st.caption("Tests hundreds of setting combinations on past 5-minute candles, but only counts a result if the winning "
               "settings also made money on later weeks they never saw (walk-forward test). It tells you plainly when "
               "there is NO edge. Needs about 3+ months of data; the more the better.")
    a, b = st.columns([2, 1])
    lsrc = a.radio("Data", ["Dhan (real market data)", "Demo - random data (should find nothing)",
                            "Demo - data with a planted edge (should find it)"], key="learn_source")
    ldays = b.slider("Calendar days of history", 60, 365, 180, key="learn_days")
    if st.button("Run research", type="primary", key="learn_run"):
        from trader.datasets import load_intraday
        from trader.intraday import synthetic_intraday

        c = cfg()
        bar = st.progress(0.0, text="Loading candles...")
        try:
            if lsrc.startswith("Dhan"):
                data = load_intraday(c, ldays, source="dhan",
                                     progress=lambda i, n, s: bar.progress(i / n * 0.3, text=f"Loaded {s} ({i}/{n})"))
            else:
                edge = 0.0004 if "planted" in lsrc else 0.0
                td = int(ldays * 5 / 7)
                data = {s: synthetic_intraday(s, td, seed=zlib.crc32(s.encode()) % 10_000 + (1 if edge else 0), edge=edge, trend=0.0)
                        for s in c["universe"]}
            if not data:
                st.error("No data loaded. Check the Setup tab.")
            else:
                res = research.run(data, c, progress=lambda i, n: bar.progress(0.3 + 0.7 * i / n, text="Testing settings..."))
                research.save_learned(res)
                st.session_state["learn"] = {"res": res, "demo": not lsrc.startswith("Dhan")}
            bar.empty()
        except Exception as e:
            bar.empty()
            st.error(f"Research failed: {e}")
    L = st.session_state.get("learn")
    if L:
        res = L["res"]
        if L["demo"]:
            st.warning("Demo data - this only shows how the researcher behaves; it says nothing about real markets.")
        {"EDGE FOUND": st.success, "UNCERTAIN": st.warning, "NO EDGE": st.error,
         "NOT ENOUGH DATA": st.info}[res["verdict"]](f"**{res['verdict']}** - {res['message']}")
        o, bl = res["oos_stats"], res.get("baseline_stats", {})
        m = st.columns(4)
        m[0].metric("Unseen-data trades", o["n"])
        m[1].metric("Avg result per trade", f"{o['mean_R']:+.2f} R")
        m[2].metric("95% range", f"{o['ci_low']:+.2f} to {o['ci_high']:+.2f}")
        m[3].metric("Your current settings", f"{bl.get('mean_R', 0):+.2f} R")
        st.caption("R = profit divided by the amount risked on the trade, after costs and slippage. "
                   f"Tested {res['n_symbols']} stocks over {res['n_days']} trading days, {res['n_combos']} setting combinations.")
        if res.get("notes"):
            st.markdown("**What the data says** (current settings, no cherry-picking)")
            for n_ in res["notes"]:
                st.markdown(f"- {n_}")
        for name, tab in res.get("insights", {}).items():
            with st.expander(f"Results by {name}"):
                st.dataframe(tab, use_container_width=True)
        if len(res.get("folds", [])):
            with st.expander("Walk-forward folds (each row: settings picked on past data, judged on the next weeks)"):
                st.dataframe(res["folds"], use_container_width=True)
        if research.applicable(res):
            st.success("These settings survived the unseen-data test:")
            st.json(res["best_params"])
            if st.button("Apply these settings to the agent", type="primary", key="learn_apply"):
                settings.save_user_overrides({"intraday": res["best_params"]})
                st.success("Applied. Paper-trade them for a few weeks before trusting them.")
        else:
            st.info("No settings are recommended, so nothing can be applied. Your current settings stay as they are.")
    prev = research.load_learned()
    if prev and not L:
        st.caption(f"Last research ({prev['when']}): {prev['verdict']} - {prev['message']}")

# ------------------------------------------------------------------ run
with tab_run:
    st.subheader("Run the agent")
    if HOSTED:
        st.info("Hosted mode: **paper trading only**. Dhan requires live orders to come from a registered static IP, "
                "which cloud hosting doesn't have. Keep this page open during market hours (09:15-15:30 IST): it checks "
                "the market every 2 minutes, tracks simulated trades, and squares them off at the configured time.")
        c = cfg()
        ready = lvl in ("ok", "soon", "unknown")
        if not ready:
            st.warning("Add a valid, unexpired Dhan token first (Secrets or Setup tab).")
        a1, a2, a3 = st.columns(3)
        if a1.button("Start paper session", type="primary", disabled=not ready, key="tick_start"):
            try:
                st.session_state["sess"] = live_mod.Session(c, False)
                st.session_state["tick_on"] = True
                st.session_state["tick_log"] = ["Session started."]
            except Exception as e:
                st.error(f"Could not start: {e}")
        if a2.button("Stop session", key="tick_stop"):
            st.session_state["tick_on"] = False
        check_now = a3.button("Check market now", key="tick_now", disabled="sess" not in st.session_state)

        @st.fragment(run_every=120)
        def ticker(force: bool = False):
            sess = st.session_state.get("sess")
            if sess is None:
                return
            if st.session_state.get("tick_on") or force:
                try:
                    status = sess.step(live_mod.now_ist())
                except Exception as e:
                    status = "error"
                    st.session_state["tick_log"].append(f"error: {e}")
                st.session_state["tick_log"] += sess.lines
                sess.lines = []
                st.session_state["tick_log"] = st.session_state["tick_log"][-200:]
                if status == "done":
                    st.session_state["tick_on"] = False
                    st.session_state["tick_log"].append("Done for today.")
            on = st.session_state.get("tick_on")
            st.write(f"Session: **{'running' if on else 'stopped'}** - last check {live_mod.now_ist():%H:%M:%S} IST")
            pos = sess.broker.positions
            st.markdown("**Open paper positions**")
            st.dataframe(pd.DataFrame(pos).T if pos else pd.DataFrame({"": ["none"]}), use_container_width=True)
            st.markdown(f"**Paper capital:** ₹{sess.broker.s['capital']:,.0f}")
            st.code("\n".join(st.session_state["tick_log"][-40:]) or "(no activity yet)", language=None)

        if "sess" in st.session_state:
            ticker(force=check_now)
        else:
            st.caption("Press Start to begin a paper session.")
    else:
        c = cfg()
        running = runner.status()
        if not running["running"]:
            mode = st.radio("Mode", ["Paper trading (simulated, no real orders)", "LIVE (real orders with real money)"],
                            key="run_mode")
            live = mode.startswith("LIVE")
            ready = lvl in ("ok", "soon", "unknown")
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
