# NSE Intraday Agent

A point-and-click dashboard that scans liquid NSE stocks for **opening-range breakouts** on 5-minute candles, takes (paper or live) trades through **Dhan**, manages stop-loss/target, and squares everything off before the close. Claude can optionally review each trade idea before it is taken.

> **Risk warning.** Most retail intraday traders lose money. This software is a tool, not advice, and has not been proven profitable. Paper-trade for weeks first. Live mode places real orders with real money; you are responsible for every trade. Automated order placement by retail clients is subject to SEBI and broker rules.

## Quick start (no command line)

1. Install **Python 3.10 or newer** from python.org. On Windows, tick **"Add Python to PATH"** in the installer.
2. Download this repository (green **Code** button, **Download ZIP**) and unzip it.
3. **Windows:** double-click `Start Trading Agent.bat`. **Mac:** double-click `Start Trading Agent.command` (first time: right-click, Open). The first start installs what it needs (a few minutes), then the dashboard opens in your browser. Keep the black window open while you use it.

## Using the dashboard

| Tab | What to do |
|---|---|
| **1 · Setup** | Paste your Dhan client ID and access token, then click **Test Dhan connection**. Check the candle times/prices against your chart. |
| **2 · Settings** | Capital, risk per trade, max positions, daily loss limit, strategy numbers, stock list, safety switches. |
| **3 · Backtest** | Replay the strategy on past candles (real Dhan data, or random demo data to try the app). |
| **4 · Run** | **Paper** or **LIVE**. Start/stop the agent, approve or reject orders, pause entries, square off. |
| **5 · History** | Trades and order approvals. |

**Every morning:** the Dhan access token lasts 24 hours. Generate a new one in your Dhan account and paste it in Setup (the top banner shows when the current one expires). Start the agent before 09:15 IST; it exits by itself after square-off (15:10).

Your credentials are saved only in a private `.env` file on your computer (git-ignored). Never share that file or paste your token into chats or issues.

## How it trades
- **Entry:** after the first 15 minutes, a candle closes beyond the opening-range high (long) or low (short), on the right side of VWAP, with volume at least 1.5x its recent average, and the range width is sensible. One attempt per stock per day, no entries after 14:30.
- **Exit:** stop-loss at the tighter of the opposite side of the range or 1.5x ATR; target at 1.5x the risk; everything squared off at 15:10.
- **Risk rules (code, not AI):** risk per trade, max open positions, max trades/day, and a daily loss limit that stops new entries. Claude can only approve or reject ideas; it can't change size or override a limit.
- **Live orders:** entry, stop-loss and target go to Dhan as one **super order**, so protection stays at the exchange even if your computer goes offline. By default each live order waits for your **Approve** click (unanswered orders expire and are not sent; end-of-day square-offs auto-proceed).
- **Safety controls:** type-to-confirm to enable live mode, pause-entries switch, square-off-now button, per-order approval, and it only ever closes positions it opened itself.

## Not yet verified against the real Dhan API
This was built and tested without access to Dhan's servers: tests use a fake Dhan client and the official SDK's source. Unverified: the candle response format and timestamps, the security-list file columns, super-order cancel behaviour, and the positions response fields. Use **Test Dhan connection** first; run paper mode for several days; try live with a tiny trading capital (a few thousand rupees) before real size. If something looks off, open an issue with the dashboard's message (without any token).

## Limits
Backtests fill at the next candle open with fixed slippage; real slippage, partial fills, circuit limits and F&O ban lists are not modelled. No holiday calendar (on holidays no data arrives). Tick size assumed 0.05. Claude has no live news. Dhan's data APIs may require an active subscription.

## For developers
```
pip install -r requirements.txt
python -m pytest -q                  # 38 tests (strategy, brokers, loop simulation, dashboard UI)
streamlit run app.py                 # dashboard
python main.py icheck RELIANCE       # CLI: connection check
python main.py ibacktest | iscan | irun [--live]
```
Layout: `app.py` dashboard · `trader/intraday.py` strategy + backtester · `trader/live.py` session loop · `trader/ibroker.py` paper + Dhan brokers · `trader/dhan_client.py` data/ids · `trader/approvals.py` order approvals · `trader/runner.py` background process · `config_intraday.yaml` defaults (your dashboard edits go to the git-ignored `config_user.yaml`).

The repo also contains an older end-of-day swing system (`main.py backtest | scan | run`, Zerodha Kite or paper) kept for reference; it is not used by the dashboard.
