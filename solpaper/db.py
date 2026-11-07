"""SQLite schema and helpers. Every decision and every gap gets a row."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
-- One row per pair ever seen (static facts + where we first saw it).
CREATE TABLE IF NOT EXISTS pairs (
    pair_address    TEXT PRIMARY KEY,
    chain           TEXT NOT NULL DEFAULT 'solana',
    dex             TEXT,
    base_mint       TEXT,
    quote_mint      TEXT,
    base_symbol     TEXT,
    name            TEXT,
    pair_created_at TEXT,          -- from the provider
    first_seen_at   TEXT NOT NULL, -- when we first saw it
    first_source    TEXT NOT NULL
);

-- Every observation of every pair: the price history replays use later.
CREATE TABLE IF NOT EXISTS pairs_seen (
    id            INTEGER PRIMARY KEY,
    pair_address  TEXT NOT NULL REFERENCES pairs(pair_address),
    observed_at   TEXT NOT NULL,   -- when we received it
    source        TEXT NOT NULL,   -- which job/provider
    data_age_s    REAL,            -- known minimum age at receipt (CDN Age + our cache); NULL = unknown
    price_usd     REAL,
    price_native  REAL,
    liquidity_usd REAL,
    fdv_usd       REAL,
    mcap_usd      REAL,
    vol_m5 REAL, vol_h1 REAL, vol_h6 REAL, vol_h24 REAL,
    buys_m5 INTEGER, sells_m5 INTEGER, buys_h1 INTEGER, sells_h1 INTEGER,
    buys_h24 INTEGER, sells_h24 INTEGER,
    chg_m5 REAL, chg_h1 REAL, chg_h24 REAL,
    volume_spike  INTEGER NOT NULL DEFAULT 0,
    unchanged     INTEGER NOT NULL DEFAULT 0  -- identical to this provider's previous reading
);
CREATE INDEX IF NOT EXISTS pairs_seen_pair_time ON pairs_seen(pair_address, observed_at);
CREATE INDEX IF NOT EXISTS pairs_seen_time ON pairs_seen(observed_at);

-- Latest response per request key, with its timestamp.
CREATE TABLE IF NOT EXISTS http_cache (
    key        TEXT PRIMARY KEY,
    provider   TEXT NOT NULL,
    url        TEXT NOT NULL,
    fetched_at REAL NOT NULL,      -- unix seconds
    status     INTEGER NOT NULL,
    cdn_age_s  REAL,               -- CDN Age header when fetched
    body       TEXT NOT NULL
);

-- Every network request we actually sent (cache hits are not requests).
CREATE TABLE IF NOT EXISTS request_log (
    id         INTEGER PRIMARY KEY,
    ts         TEXT NOT NULL,
    provider   TEXT NOT NULL,
    bucket     TEXT NOT NULL,
    url        TEXT NOT NULL,
    status     INTEGER,            -- NULL = network error / timeout
    ms         REAL,
    bytes      INTEGER,
    age_s      REAL,               -- CDN Age header
    error      TEXT,
    limits     TEXT                -- provider-reported rate-limit headers, JSON
);

-- Outages and rate-limit periods. Nothing is fabricated to fill them.
CREATE TABLE IF NOT EXISTS data_gaps (
    id          INTEGER PRIMARY KEY,
    provider    TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,              -- NULL while ongoing
    attempts    INTEGER NOT NULL,
    last_status INTEGER,
    last_error  TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY,
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    message TEXT NOT NULL,
    data    TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Add columns that CREATE TABLE IF NOT EXISTS won't add to an existing table."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(pairs_seen)")}
    if "unchanged" not in cols:
        conn.execute("ALTER TABLE pairs_seen ADD COLUMN unchanged INTEGER NOT NULL DEFAULT 0")


def event(conn: sqlite3.Connection, kind: str, message: str, data: dict | None = None):
    conn.execute("INSERT INTO events (ts, kind, message, data) VALUES (?, ?, ?, ?)",
                 (now_iso(), kind, message, json.dumps(data) if data else None))
