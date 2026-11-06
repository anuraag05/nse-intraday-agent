import base64
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from trader import approvals, runner, settings
from trader.dhan_client import save_env, token_expiry

ROOT = Path(__file__).resolve().parent.parent


def fake_jwt(exp: int) -> str:
    b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{b({'alg': 'HS512'})}.{b({'exp': exp, 'dhanClientId': '1'})}.sig"


def test_token_expiry_parsing():
    exp = int((datetime.now(timezone.utc) + timedelta(hours=10)).timestamp())
    got = token_expiry(fake_jwt(exp))
    assert got is not None and abs((got - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds() - 36000) < 5
    assert token_expiry("garbage") is None and token_expiry(None) is None


def test_save_env_updates_and_preserves(tmp_path, monkeypatch):
    p = tmp_path / ".env"
    p.write_text("# comment\nDHAN_CLIENT_ID=old\nOTHER=keep\n")
    save_env({"DHAN_CLIENT_ID": "new", "DHAN_ACCESS_TOKEN": "tok"}, str(p))
    lines = p.read_text().splitlines()
    assert "# comment" in lines and "OTHER=keep" in lines
    assert "DHAN_CLIENT_ID=new" in lines and "DHAN_ACCESS_TOKEN=tok" in lines
    assert os.environ["DHAN_ACCESS_TOKEN"] == "tok"
    for k in ("DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN"):
        monkeypatch.delenv(k, raising=False)


def test_settings_merge_and_reset(tmp_path):
    (tmp_path / "config_intraday.yaml").write_text("risk: {a: 1, b: 2}\nuniverse: [X]\n")
    settings.save_user_overrides({"risk": {"b": 9}}, tmp_path)
    cfg = settings.load_intraday_cfg(tmp_path)
    assert cfg["risk"] == {"a": 1, "b": 9} and cfg["universe"] == ["X"]
    settings.reset_user_overrides(tmp_path)
    assert settings.load_intraday_cfg(tmp_path)["risk"]["b"] == 2


def test_live_override_only_via_env(tmp_path, monkeypatch):
    (tmp_path / "config_intraday.yaml").write_text("mode: paper\n")
    monkeypatch.delenv("AGENT_GUI_LIVE", raising=False)
    assert settings.load_intraday_cfg(tmp_path)["mode"] == "paper"
    monkeypatch.setenv("AGENT_GUI_LIVE", "1")
    assert settings.load_intraday_cfg(tmp_path)["mode"] == "live"


def test_approvals_approve_reject_timeout(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_GUI", "1")

    def decider(approve):
        for _ in range(50):
            p = approvals.pending()
            if p:
                approvals.decide(p[0]["id"], approve)
                return
            time.sleep(0.05)

    for approve in (True, False):
        t = threading.Thread(target=decider, args=(approve,))
        t.start()
        assert approvals.request("order", timeout=5, poll=0.05) is approve
        t.join()
    assert approvals.request("order", timeout=0.2, poll=0.05) is False            # timeout -> reject
    assert approvals.request("squareoff", timeout=0.2, poll=0.05, default_on_timeout=True) is True
    assert approvals.pending() == []
    assert approvals.decide("nope", True) is False


def test_runner_start_stop_status(tmp_path):
    assert runner.status(tmp_path)["running"] is False
    info = runner.start(False, tmp_path, cmd=[sys.executable, "-c", "import time; print('hi', flush=True); time.sleep(60)"])
    try:
        time.sleep(0.5)
        st = runner.status(tmp_path)
        assert st["running"] and st["pid"] == info["pid"]
        with pytest.raises(RuntimeError):
            runner.start(False, tmp_path, cmd=[sys.executable, "-c", "pass"])
        assert "hi" in runner.tail_log(tmp_path)
    finally:
        assert runner.stop(tmp_path) is True
    assert runner.status(tmp_path)["running"] is False
    assert runner.stop(tmp_path) is False


def test_runner_passes_live_env_only_when_live(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_CONFIRM", "YES")  # must be stripped for paper runs
    code = "import os;print(os.environ.get('LIVE_TRADING_CONFIRM'), os.environ.get('AGENT_GUI'), os.environ.get('AGENT_GUI_LIVE'))"
    runner.start(False, tmp_path, cmd=[sys.executable, "-c", code])
    time.sleep(1)
    assert "None 1 None" in runner.tail_log(tmp_path)
    runner.start(True, tmp_path, cmd=[sys.executable, "-c", code])
    time.sleep(1)
    assert "YES 1 1" in runner.tail_log(tmp_path)


def test_no_secrets_in_tracked_files():
    import re

    jwt = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")  # a real access token
    key = re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")
    for p in ROOT.rglob("*"):
        if p.is_file() and ".venv" not in p.parts and p.suffix in {".py", ".yaml", ".md", ".txt", ".bat", ".command", ".toml", ".example"}:
            txt = p.read_text(errors="ignore")
            assert not jwt.search(txt) and not key.search(txt), f"secret-looking text in {p}"
