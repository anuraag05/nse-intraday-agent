"""Config = config_intraday.yaml (defaults, tracked in git) overlaid with config_user.yaml (your dashboard edits)."""
from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
BASE, USER = "config_intraday.yaml", "config_user.yaml"


def deep_merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in (b or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) or {} if path.exists() else {}


def load_intraday_cfg(root: Path = ROOT) -> dict:
    cfg = deep_merge(_load(root / BASE), _load(root / USER))
    if os.environ.get("AGENT_GUI_LIVE") == "1":  # set only by the dashboard after its live confirmation
        cfg["mode"] = "live"
    return cfg


def save_user_overrides(overrides: dict, root: Path = ROOT) -> None:
    merged = deep_merge(_load(root / USER), overrides)
    (root / USER).write_text("# Your dashboard settings (override config_intraday.yaml)\n" + yaml.safe_dump(merged, sort_keys=False))


def reset_user_overrides(root: Path = ROOT) -> None:
    p = root / USER
    if p.exists():
        p.unlink()
