"""Per-order approvals. In the dashboard (AGENT_GUI=1) an order waits for an Approve/Reject click;
in a terminal it asks for 'yes'. Requests live as small JSON files in state/approvals/."""
from __future__ import annotations

import json
import os
import time
import uuid

DIR = os.path.join("state", "approvals")


def gui_mode() -> bool:
    return os.environ.get("AGENT_GUI") == "1"


def _write(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _read(path: str) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def request(text: str, timeout: float = 120, default_on_timeout: bool = False, poll: float = 1.0) -> bool:
    """Ask the human to approve an order. Returns True only on explicit approval (or on timeout if
    default_on_timeout, which is used for risk-reducing square-offs)."""
    if not gui_mode():
        return input(f"LIVE ORDER: {text}. Type 'yes' to send: ").strip().lower() == "yes"
    os.makedirs(DIR, exist_ok=True)
    rid = uuid.uuid4().hex[:10]
    path = os.path.join(DIR, rid + ".json")
    _write(path, {"id": rid, "text": text, "status": "pending", "created": time.time(), "timeout": timeout,
                  "default_on_timeout": default_on_timeout})
    end = time.time() + timeout
    while time.time() < end:
        d = _read(path)
        if d and d["status"] in ("approved", "rejected"):
            return d["status"] == "approved"
        time.sleep(poll)
    d = _read(path) or {}
    if d.get("status") == "pending":
        d["status"] = "auto-approved" if default_on_timeout else "expired"
        _write(path, d)
    return default_on_timeout


def pending() -> list[dict]:
    out = []
    if not os.path.isdir(DIR):
        return out
    for fn in sorted(os.listdir(DIR)):
        if fn.endswith(".json"):
            d = _read(os.path.join(DIR, fn))
            if d and d.get("status") == "pending" and time.time() < d["created"] + d["timeout"]:
                out.append(d)
    return out


def decide(rid: str, approve: bool) -> bool:
    path = os.path.join(DIR, rid + ".json")
    d = _read(path)
    if not d or d.get("status") != "pending":
        return False
    d["status"] = "approved" if approve else "rejected"
    _write(path, d)
    return True


def recent(n: int = 20) -> list[dict]:
    if not os.path.isdir(DIR):
        return []
    rows = [d for fn in os.listdir(DIR) if fn.endswith(".json") and (d := _read(os.path.join(DIR, fn)))]
    return sorted(rows, key=lambda d: d["created"], reverse=True)[:n]
