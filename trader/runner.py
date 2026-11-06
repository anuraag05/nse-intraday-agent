"""Start/stop the trading loop as a background process so the dashboard (or you closing a browser tab)
doesn't interrupt it. State: state/agent.pid, log: state/agent.log."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent


def _paths(root: Path):
    return root / "state" / "agent.pid", root / "state" / "agent.log"


def status(root: Path = ROOT) -> dict:
    pidf, _ = _paths(root)
    try:
        info = json.loads(pidf.read_text())
        proc = psutil.Process(info["pid"])
        if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE:
            return {"running": True, **info}
    except (OSError, ValueError, KeyError, psutil.Error):
        pass
    return {"running": False}


def start(live: bool, root: Path = ROOT, cmd: list[str] | None = None) -> dict:
    if status(root)["running"]:
        raise RuntimeError("The agent is already running.")
    pidf, logf = _paths(root)
    pidf.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(AGENT_GUI="1", PYTHONUNBUFFERED="1")
    if live:
        env.update(AGENT_GUI_LIVE="1", LIVE_TRADING_CONFIRM="YES")
    else:
        env.pop("AGENT_GUI_LIVE", None)
        env.pop("LIVE_TRADING_CONFIRM", None)
    cmd = cmd or [sys.executable, "-u", "main.py", "irun"] + (["--live"] if live else [])
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with open(logf, "w") as log:
        p = subprocess.Popen(cmd, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, creationflags=flags)
    info = {"pid": p.pid, "live": live, "started": time.time()}
    pidf.write_text(json.dumps(info))
    return info


def stop(root: Path = ROOT, wait: float = 5.0) -> bool:
    st = status(root)
    if not st["running"]:
        return False
    try:
        proc = psutil.Process(st["pid"])
        procs = proc.children(recursive=True) + [proc]
        for p in procs:
            p.terminate()
        _, alive = psutil.wait_procs(procs, timeout=wait)
        for p in alive:
            p.kill()
    except psutil.Error:
        pass
    return True


def tail_log(root: Path = ROOT, n: int = 60) -> str:
    _, logf = _paths(root)
    try:
        return "".join(logf.read_text(errors="replace").splitlines(keepends=True)[-n:])
    except OSError:
        return ""
