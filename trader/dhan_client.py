"""DhanHQ helpers: credentials, security-id lookup, 5-minute candle download.

Credentials are read from environment variables (or a local .env file), never from config.yaml:
    DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

IST_OFFSET = pd.Timedelta(hours=5, minutes=30)
_MIN_GAP = 0.25  # seconds between data calls, to stay well inside Dhan's rate limits


def load_env(path: str = ".env") -> None:
    """Tiny .env loader (KEY=VALUE per line). Existing environment variables win."""
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def save_env(values: dict, path: str = ".env") -> None:
    """Create/update KEY=VALUE lines in .env (kept private, git-ignored) and apply them to this process."""
    lines = open(path).read().splitlines() if os.path.exists(path) else []
    done = set()
    for i, line in enumerate(lines):
        k = line.split("=", 1)[0].strip()
        if k in values and not line.strip().startswith("#"):
            lines[i], done = f"{k}={values[k]}", done | {k}
    lines += [f"{k}={v}" for k, v in values.items() if k not in done]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    os.environ.update({k: str(v) for k, v in values.items()})


def token_expiry(token: str | None) -> datetime | None:
    """Expiry time (UTC) read from the JWT payload; no network, no verification."""
    import base64
    import json

    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload))["exp"]
        return datetime.fromtimestamp(int(exp), timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


def make_dhan():
    load_env()
    cid, tok = os.environ.get("DHAN_CLIENT_ID"), os.environ.get("DHAN_ACCESS_TOKEN")
    if not cid or not tok:
        raise RuntimeError("Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN (env vars or .env file).")
    from dhanhq import DhanContext, dhanhq  # pip install dhanhq

    return dhanhq(DhanContext(cid, tok))


# ---------- security ids ----------
def _col(df: pd.DataFrame, *needles: str) -> str | None:
    for c in df.columns:
        if all(n in c.upper() for n in needles):
            return c
    return None


def resolve_security_ids(dhan, symbols: list[str], overrides: dict | None = None,
                         cache: str = "state/security_list.csv") -> dict[str, str]:
    """Map NSE equity symbols to Dhan security ids. Config overrides win; the rest come from Dhan's scrip master."""
    ids = {k: str(v) for k, v in (overrides or {}).items()}
    missing = [s for s in symbols if s not in ids]
    if not missing:
        return ids
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    fresh = os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 86400
    df = pd.read_csv(cache, low_memory=False) if fresh else type(dhan).fetch_security_list("compact", cache)
    if df is None or df.empty:
        raise RuntimeError("Could not download Dhan's security list. Add ids under dhan.security_ids in the config.")
    exch, sid = _col(df, "EXCH", "ID"), _col(df, "SECURITY", "ID")
    sym, inst = _col(df, "TRADING", "SYMBOL"), _col(df, "INSTRUMENT", "NAME")
    series = _col(df, "SERIES")
    if not all([exch, sid, sym]):
        raise RuntimeError(f"Unexpected security list columns: {list(df.columns)[:12]}")
    q = df[(df[exch].astype(str).str.upper() == "NSE")]
    if inst:
        q = q[q[inst].astype(str).str.upper() == "EQUITY"]
    if series:
        q = q[q[series].astype(str).str.upper() == "EQ"]
    found = dict(zip(q[sym].astype(str).str.upper(), q[sid].astype(str)))
    still = []
    for s in missing:
        if s.upper() in found:
            ids[s] = found[s.upper()].split(".")[0]
        else:
            still.append(s)
    if still:
        raise RuntimeError(f"No NSE equity security id found for {still}. Add them under dhan.security_ids.")
    return ids


# ---------- candles ----------
def parse_candles(resp: dict) -> pd.DataFrame:
    """Dhan intraday response -> DataFrame indexed by IST candle time (naive)."""
    if not isinstance(resp, dict) or resp.get("status") != "success":
        raise RuntimeError(f"Dhan data error: {resp.get('remarks') if isinstance(resp, dict) else resp}")
    d = resp["data"]
    if isinstance(d, dict) and "open" not in d and "data" in d:
        d = d["data"]
    if not d or "timestamp" not in d or len(d["timestamp"]) == 0:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    ts = pd.to_datetime(pd.Series(d["timestamp"]), unit="s") + IST_OFFSET
    if ts.min().year < 2000:
        raise RuntimeError("Unexpected timestamp format in Dhan response; run `main.py icheck` and report it.")
    df = pd.DataFrame({k: pd.Series(d[k], dtype=float) for k in ["open", "high", "low", "close", "volume"]})
    df.index = pd.DatetimeIndex(ts, name="ts")
    return df[~df.index.duplicated()].sort_index()


def fetch_candles(dhan, security_id: str, start: datetime, end: datetime, interval: int = 5) -> pd.DataFrame:
    time.sleep(_MIN_GAP)
    resp = dhan.intraday_minute_data(
        str(security_id), dhan.NSE, "EQUITY",
        start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S"), interval=interval,
    )
    return parse_candles(resp)


def fetch_history(dhan, security_id: str, days: int, interval: int = 5, chunk_days: int = 30) -> pd.DataFrame:
    """Download `days` calendar days of candles in chunks. Dhan may return less history than requested."""
    end = datetime.now().replace(hour=15, minute=30, second=0, microsecond=0)
    frames, cur = [], end - timedelta(days=days)
    while cur < end:
        nxt = min(cur + timedelta(days=chunk_days), end)
        f = fetch_candles(dhan, security_id, cur.replace(hour=9, minute=15, second=0), nxt, interval)
        if not f.empty:
            frames.append(f)
        cur = nxt
    if not frames:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    out = pd.concat(frames)
    return out[~out.index.duplicated()].sort_index()
