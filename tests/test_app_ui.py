"""Headless UI tests of the Streamlit dashboard (no browser, no network)."""
import os
import shutil
import sys
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def at(tmp_path, monkeypatch):
    # run the app against a scratch copy so state/config files never touch the real project
    work = tmp_path / "proj"
    shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns(".venv", "state", "__pycache__", ".git", ".env", "config_user.yaml"))
    monkeypatch.chdir(work)
    from trader import settings as _settings
    monkeypatch.setattr(_settings, "ROOT", work)
    for k in ("DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    sys.modules.pop("app", None)
    app = AppTest.from_file(str(work / "app.py"), default_timeout=60)
    app.run()
    return app


def test_renders_without_errors(at):
    assert not at.exception
    assert len(at.tabs) == 5
    assert any("No Dhan credentials" in w.value for w in at.warning)


def test_save_credentials_then_status_updates(at):
    at.text_input(key="cid").set_value("12345")
    at.text_input(key="tok").set_value("header.payload.sig")
    next(b for b in at.button if b.label == "Save credentials").click().run()
    assert not at.exception
    assert os.environ.get("DHAN_CLIENT_ID") == "12345"
    assert (Path.cwd() / ".env").exists()


def test_demo_backtest_runs_and_shows_metrics(at):
    at.radio(key="bt_source").set_value("Demo (random data - only to try the app)")
    at.slider(key="bt_days").set_value(20)
    at.button(key="bt_run").click().run(timeout=120)
    assert not at.exception, at.exception
    assert len(at.metric) == 5
    assert any("random" in w.value for w in at.warning)


def test_settings_validation_blocks_bad_times(at):
    # cutoff later than square-off must be rejected
    for ti in at.text_input:
        if ti.label.startswith("No new entries"):
            ti.set_value("15:20")
    next(b for b in at.button if b.label == "Save settings").click().run()
    assert any("earlier" in e.value for e in at.error), [e.value for e in at.error]
    assert not (Path.cwd() / "config_user.yaml").exists()


def test_start_disabled_without_credentials(at):
    start = at.button(key="start_btn")
    assert start.disabled


# ---------------- hosted mode ----------------
def _hosted_app(tmp_path, monkeypatch, **env):
    work = tmp_path / "proj"
    shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns(".venv", "state", "__pycache__", ".git", ".env", "config_user.yaml"))
    monkeypatch.chdir(work)
    from trader import settings as _settings
    monkeypatch.setattr(_settings, "ROOT", work)
    for k in ("DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN", "ANTHROPIC_API_KEY", "APP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOSTED", "1")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return AppTest.from_file(str(work / "app.py"), default_timeout=60)


def test_hosted_has_no_live_option(tmp_path, monkeypatch):
    app = _hosted_app(tmp_path, monkeypatch)
    app.run()
    assert not app.exception
    assert not any("LIVE" in str(r.options) for r in app.radio if r.key == "run_mode")
    assert any("paper trading only" in i.value for i in app.info)
    assert any("Anyone with this page" in w.value for w in app.warning)       # no password set


def test_password_gate(tmp_path, monkeypatch):
    app = _hosted_app(tmp_path, monkeypatch, APP_PASSWORD="s3cret")
    app.run()
    assert len(app.tabs) == 0                                                  # nothing shown before login
    app.text_input(key="pw").set_value("wrong").run()
    assert any("Wrong password" in e.value for e in app.error) and len(app.tabs) == 0
    app.text_input(key="pw").set_value("s3cret").run()
    assert len(app.tabs) == 5 and not app.exception


def test_hosted_paper_session_steps_through_the_day(tmp_path, monkeypatch):
    from datetime import datetime

    import pandas as pd
    from test_intraday import make_day
    from test_live_loop import Clock, FeedDhan

    from trader import live

    clock = Clock(datetime(2026, 3, 2, 10, 5, 30))          # just after the breakout candle closes
    days = [make_day(f"2026-02-{d}", breakout=False) for d in (24, 25, 26, 27)] + [make_day("2026-03-02")]
    feed = FeedDhan(pd.concat(days), clock)
    monkeypatch.setattr(live, "now_ist", lambda: clock.t)
    monkeypatch.setattr(live.time, "sleep", lambda s: None)
    monkeypatch.setattr(live, "make_dhan", lambda: feed)
    app = _hosted_app(tmp_path, monkeypatch, DHAN_CLIENT_ID="1", DHAN_ACCESS_TOKEN="a.b.c")
    (Path.cwd() / "config_user.yaml").write_text(
        "universe: [X]\ndhan: {security_ids: {X: '1333'}}\nclaude_review: {enabled: false}\n")
    app.run()
    # token is not a valid JWT -> status 'unknown' still allows starting
    app.button(key="tick_start").click().run()
    assert not app.exception, app.exception
    text = " ".join(c.value for c in app.code)
    assert "X long" in text and "APPROVE" in text, text
    pos_text = str(app.dataframe[-1].value) if app.dataframe else ""
    assert "long" in pos_text
    # later in the day: square-off flattens it and the session ends
    clock.t = datetime(2026, 3, 2, 15, 12)
    app.button(key="tick_now").click().run()
    assert not app.exception
    assert any("Done for today" in c.value for c in app.code)
