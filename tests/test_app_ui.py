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
