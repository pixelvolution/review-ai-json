"""Load config.yaml (plain dict access; no magic)."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.yaml"


def load_config(path: str | os.PathLike | None = None) -> dict:
    p = Path(path or os.environ.get("SOLPAPER_CONFIG") or DEFAULT_CONFIG)
    cfg = yaml.safe_load(p.read_text())
    db = Path(os.environ.get("SOLPAPER_DB") or cfg.get("db_path", "solpaper.db"))
    cfg["db_path"] = str(db if db.is_absolute() else p.parent / db)
    return cfg
