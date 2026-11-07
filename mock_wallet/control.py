"""Control database: suggestions, validation evidence, and the config change log.

This lives in its own SQLite file (``control.db``). The trading engine
(wallet, trader, sizer, market, replay, research/engine, strategies) never
opens it, so a suggestion can't change trading behaviour. A test checks this.

Suggestion statuses:
    pending          written by the MCP tool, untested
    insufficient data  too few trades to judge; never passes
    likely noise     won one window but not the other
    rejected         won neither window, or drawdown got worse
    shadow           passed both windows; now running forward in a shadow wallet
    validated        passed (and passed shadow, if enabled). Shown for approval.
    applied          a person applied it with set_config.py
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_CONTROL_DB = HERE / "control.db"
SETTINGS_PATH = HERE / "validation.json"

STATUSES = ("pending", "insufficient data", "likely noise", "rejected", "shadow",
            "validated", "applied")

SCHEMA = """
CREATE TABLE IF NOT EXISTS suggestions (
    id             INTEGER PRIMARY KEY,
    created_at     TEXT NOT NULL,
    parameter      TEXT NOT NULL,
    proposed_value TEXT NOT NULL,          -- JSON
    rationale      TEXT NOT NULL,
    derived_start  TEXT NOT NULL,          -- window A: the data it came from
    derived_end    TEXT NOT NULL,
    config_sha     TEXT NOT NULL,          -- live config it was proposed against
    status         TEXT NOT NULL DEFAULT 'pending',
    status_at      TEXT NOT NULL,
    status_reason  TEXT,
    shadow_started_at TEXT
);

CREATE TABLE IF NOT EXISTS validations (
    id             INTEGER PRIMARY KEY,
    suggestion_id  INTEGER NOT NULL REFERENCES suggestions(id),
    ts             TEXT NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN ('test', 'shadow')),
    tests_tried    INTEGER NOT NULL,       -- counter value including this test
    required_margin REAL NOT NULL,
    verdict        TEXT NOT NULL,
    reason         TEXT NOT NULL,
    evidence       TEXT NOT NULL           -- JSON: per-window baseline vs candidate
);

CREATE TABLE IF NOT EXISTS config_changes (
    id            INTEGER PRIMARY KEY,
    ts            TEXT NOT NULL,
    source        TEXT NOT NULL,           -- set_config | manual edit detected | initial
    parameter     TEXT,
    old_value     TEXT,
    new_value     TEXT,
    reason        TEXT,
    suggestion_id INTEGER,
    config_sha    TEXT NOT NULL,
    config        TEXT NOT NULL            -- full config after the change
);

CREATE TRIGGER IF NOT EXISTS suggestions_no_delete BEFORE DELETE ON suggestions
BEGIN SELECT RAISE(ABORT, 'suggestions are permanent history'); END;
CREATE TRIGGER IF NOT EXISTS suggestions_fixed_fields BEFORE UPDATE OF
    parameter, proposed_value, rationale, derived_start, derived_end, created_at, config_sha
    ON suggestions
BEGIN SELECT RAISE(ABORT, 'a suggestion cannot be edited; make a new one'); END;
CREATE TRIGGER IF NOT EXISTS validations_no_update BEFORE UPDATE ON validations
BEGIN SELECT RAISE(ABORT, 'validation evidence is append-only'); END;
CREATE TRIGGER IF NOT EXISTS validations_no_delete BEFORE DELETE ON validations
BEGIN SELECT RAISE(ABORT, 'validation evidence is append-only'); END;
CREATE TRIGGER IF NOT EXISTS config_changes_no_update BEFORE UPDATE ON config_changes
BEGIN SELECT RAISE(ABORT, 'the config change log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS config_changes_no_delete BEFORE DELETE ON config_changes
BEGIN SELECT RAISE(ABORT, 'the config change log is append-only'); END;
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def control_db_path(path=None) -> Path:
    return Path(path or os.environ.get("MOCK_WALLET_CONTROL_DB") or DEFAULT_CONTROL_DB)


def connect(path=None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(control_db_path(path)))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def load_settings(path=None) -> dict:
    p = Path(path or os.environ.get("MOCK_WALLET_VALIDATION") or SETTINGS_PATH)
    return json.loads(p.read_text())


# --------------------------------------------------------------- suggestions

def add_suggestion(parameter, proposed_value, rationale, derived_start=None,
                   derived_end=None, db=None, live_config_path=None, settings=None) -> dict:
    """Record a pending suggestion. Validates the parameter; never applies it."""
    import live_config
    cfg = live_config.load(live_config_path)
    name = live_config.normalize_parameter(cfg, parameter)
    value = live_config.coerce_value(cfg, name, proposed_value)
    if not str(rationale or "").strip():
        raise ValueError("a rationale is required")
    s = settings or load_settings()
    start = derived_start or s["research_window"][0]
    end = derived_end or s["research_window"][1]
    if str(start) >= str(end):
        raise ValueError("derived window start must be before its end")
    ts = now_iso()
    conn = connect(db)
    with conn:
        cur = conn.execute(
            "INSERT INTO suggestions (created_at, parameter, proposed_value, rationale, "
            "derived_start, derived_end, config_sha, status, status_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (ts, name, json.dumps(value), rationale.strip(), str(start), str(end),
             live_config.sha(cfg), ts))
    sid = cur.lastrowid
    conn.close()
    return get_suggestion(sid, db)


def get_suggestion(sid: int, db=None) -> dict:
    conn = connect(db)
    try:
        row = conn.execute("SELECT * FROM suggestions WHERE id = ?", (sid,)).fetchone()
        if row is None:
            raise KeyError(f"no suggestion {sid}")
        d = dict(row)
        d["proposed_value"] = json.loads(d["proposed_value"])
        return d
    finally:
        conn.close()


def list_suggestions(status=None, db=None) -> list[dict]:
    conn = connect(db)
    try:
        q = "SELECT id FROM suggestions" + (" WHERE status = ?" if status else "") + " ORDER BY id"
        ids = [r["id"] for r in conn.execute(q, (status,) if status else ())]
    finally:
        conn.close()
    return [get_suggestion(i, db) for i in ids]


def set_status(sid: int, status: str, reason: str, db=None, shadow_started_at=None):
    if status not in STATUSES:
        raise ValueError(status)
    conn = connect(db)
    with conn:
        conn.execute(
            "UPDATE suggestions SET status = ?, status_at = ?, status_reason = ?, "
            "shadow_started_at = COALESCE(?, shadow_started_at) WHERE id = ?",
            (status, now_iso(), reason, shadow_started_at, sid))
    conn.close()


# --------------------------------------------------------------- validations

def tests_tried(db=None) -> int:
    """Every suggestion test ever run (re-tests count again)."""
    conn = connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM validations WHERE kind = 'test'").fetchone()[0]
    finally:
        conn.close()


def record_validation(sid, kind, tests_tried_n, required_margin, verdict, reason, evidence,
                      db=None) -> int:
    conn = connect(db)
    with conn:
        cur = conn.execute(
            "INSERT INTO validations (suggestion_id, ts, kind, tests_tried, required_margin, "
            "verdict, reason, evidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, now_iso(), kind, tests_tried_n, required_margin, verdict, reason,
             json.dumps(evidence, default=str)))
    conn.close()
    return cur.lastrowid


def latest_validation(sid, kind="test", db=None) -> dict | None:
    conn = connect(db)
    try:
        r = conn.execute("SELECT * FROM validations WHERE suggestion_id = ? AND kind = ? "
                         "ORDER BY id DESC LIMIT 1", (sid, kind)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["evidence"] = json.loads(d["evidence"])
        return d
    finally:
        conn.close()


# --------------------------------------------------------------- config log

def log_config_change(source, cfg, parameter=None, old=None, new=None, reason=None,
                      suggestion_id=None, db=None):
    import live_config
    conn = connect(db)
    with conn:
        conn.execute(
            "INSERT INTO config_changes (ts, source, parameter, old_value, new_value, reason, "
            "suggestion_id, config_sha, config) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now_iso(), source, parameter, json.dumps(old), json.dumps(new), reason,
             suggestion_id, live_config.sha(cfg), json.dumps(cfg, sort_keys=True)))
    conn.close()


def config_changes(db=None, limit=50) -> list[dict]:
    conn = connect(db)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM config_changes ORDER BY id DESC LIMIT ?", (limit,))]
    finally:
        conn.close()


def check_unlogged_edits(db=None, live_config_path=None, log=True) -> list[str]:
    """Compare the live config file with the last logged version.

    Logs any difference as 'manual edit detected' (one row per changed
    field), or the first snapshot as 'initial'. Returns the changed fields.
    """
    import live_config
    cfg = live_config.load(live_config_path)
    conn = connect(db)
    try:
        last = conn.execute("SELECT config, config_sha FROM config_changes "
                            "ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    if last is None:
        if log:
            log_config_change("initial", cfg, reason="first config snapshot", db=db)
        return []
    if last["config_sha"] == live_config.sha(cfg):
        return []
    prev = json.loads(last["config"])
    changed = []
    for key in sorted(set(prev) | set(cfg)):
        if key == "params":
            for k in sorted(set(prev.get("params", {})) | set(cfg.get("params", {}))):
                a, b = prev.get("params", {}).get(k), cfg.get("params", {}).get(k)
                if a != b:
                    changed.append((f"params.{k}", a, b))
        elif prev.get(key) != cfg.get(key):
            changed.append((key, prev.get(key), cfg.get(key)))
    if log:
        for name, a, b in changed:
            log_config_change("manual edit detected", cfg, name, a, b,
                              reason="live_config.json was edited by hand", db=db)
    return [c[0] for c in changed]
