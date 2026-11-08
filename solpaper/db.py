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

-- Phase 2: filter decisions. Rows are written when a pair's outcome changes.
CREATE TABLE IF NOT EXISTS skips (
    id           INTEGER PRIMARY KEY,
    pair_address TEXT NOT NULL,
    ts           TEXT NOT NULL,
    stage        TEXT NOT NULL,     -- tier1 | token | sell_route
    reason       TEXT NOT NULL,     -- machine code, e.g. liquidity_below_floor
    detail       TEXT NOT NULL,     -- human-readable, with the numbers used
    metrics      TEXT               -- JSON
);
CREATE INDEX IF NOT EXISTS skips_pair ON skips(pair_address);

CREATE TABLE IF NOT EXISTS filter_passes (
    id           INTEGER PRIMARY KEY,
    pair_address TEXT NOT NULL,
    ts           TEXT NOT NULL,
    metrics      TEXT
);

CREATE TABLE IF NOT EXISTS filter_state (
    pair_address TEXT PRIMARY KEY,
    outcome      TEXT NOT NULL,     -- pass | skip
    reason       TEXT,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS token_checks (
    mint             TEXT PRIMARY KEY,
    checked_at       REAL NOT NULL,  -- unix seconds
    readable         INTEGER NOT NULL,
    program          TEXT,
    decimals         INTEGER,
    mint_authority   TEXT,
    freeze_authority TEXT,
    extensions       TEXT            -- JSON: name -> state
);

CREATE TABLE IF NOT EXISTS sell_quotes (
    id                INTEGER PRIMARY KEY,
    pair_address      TEXT NOT NULL,
    mint              TEXT NOT NULL,
    ts                TEXT NOT NULL,
    checked_at        REAL NOT NULL,
    usd_in            REAL NOT NULL,
    amount_in         INTEGER,
    out_mint          TEXT,
    out_amount        INTEGER,
    price_impact      REAL,             -- Jupiter-reported (cross-check); NULL if absent
    ref_usd_in        REAL,             -- tiny reference sale (amendment A3)
    ref_amount_in     INTEGER,
    ref_out_amount    INTEGER,
    measured_impact   REAL,             -- from payouts: what the decision uses
    impact_disagrees  INTEGER NOT NULL DEFAULT 0,
    effective_vs_pool REAL,
    routes            TEXT,
    ok                INTEGER NOT NULL,
    reason            TEXT
);
CREATE INDEX IF NOT EXISTS sell_quotes_mint ON sell_quotes(mint, checked_at);

-- Phase 3: trade-level records (schema: specs/solpaper-phase3-research/TRADE_SCHEMA.md).
CREATE TABLE IF NOT EXISTS trade_records (
    trade_id     TEXT PRIMARY KEY,
    pair_address TEXT NOT NULL,
    t            REAL NOT NULL,      -- block time, unix seconds
    block_number INTEGER,
    tx_hash      TEXT,
    wallet       TEXT,
    kind         TEXT,
    volume_usd   REAL,
    price_usd    REAL,
    base_amount  REAL,
    fetched_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS trade_records_pair ON trade_records(pair_address, t);

CREATE TABLE IF NOT EXISTS wash_checks (
    id           INTEGER PRIMARY KEY,
    pair_address TEXT NOT NULL,
    checked_at   REAL NOT NULL,
    as_of        REAL NOT NULL,      -- only trades at or before this were used
    status       TEXT NOT NULL,      -- ok | insufficient_trades | unavailable
    score        REAL,
    flagged      INTEGER,
    n_trades     INTEGER,
    metrics      TEXT,               -- JSON: metrics, components, unavailable parts
    source       TEXT NOT NULL       -- engine | research
);
CREATE INDEX IF NOT EXISTS wash_checks_pair ON wash_checks(pair_address, checked_at);

CREATE TABLE IF NOT EXISTS holder_checks (
    id                INTEGER PRIMARY KEY,
    pair_address      TEXT NOT NULL,
    mint              TEXT NOT NULL,
    checked_at        REAL NOT NULL,
    top_share         REAL,           -- top-10 holders' share of supply
    top_share_ex_pair REAL,           -- same, excluding accounts owned by the pair itself
    largest_share     REAL,
    reason            TEXT            -- why NULL, when it is
);

CREATE TABLE IF NOT EXISTS research_runs (
    run_id     INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    seed       TEXT NOT NULL,
    config     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_sample (
    run_id       INTEGER NOT NULL,
    pair_address TEXT NOT NULL,
    sample_u     REAL NOT NULL,
    PRIMARY KEY (run_id, pair_address)
);
CREATE TABLE IF NOT EXISTS research_results (
    id             INTEGER PRIMARY KEY,
    run_id         INTEGER NOT NULL,
    pair_address   TEXT NOT NULL,
    rule_id        TEXT NOT NULL,
    status         TEXT NOT NULL,
    wash_status    TEXT,
    wash_flagged   INTEGER,
    activity_bucket TEXT,
    concentration_bucket TEXT,
    entry_t        REAL,
    exit_t         REAL,
    exit_reason    TEXT,
    honeypot       INTEGER,
    net            REAL,
    price_only_net REAL
);
-- Every comparison or rule variation ever tried, for multiple-testing correction.
CREATE TABLE IF NOT EXISTS research_hypotheses (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER NOT NULL,
    ts          TEXT NOT NULL,
    rule_id     TEXT NOT NULL,
    bucket      TEXT NOT NULL,
    n_pairs     INTEGER NOT NULL,
    share_positive REAL,
    median_net  REAL,
    verdict     TEXT NOT NULL
);

-- Phase 4: paper executor. Every decision has a reason.
CREATE TABLE IF NOT EXISTS signals (
    id           INTEGER PRIMARY KEY,
    pair_address TEXT NOT NULL,
    t            REAL NOT NULL,      -- decision time, unix seconds
    kind         TEXT NOT NULL,      -- entry | exit
    reason       TEXT NOT NULL,
    price        REAL,               -- latest observed mid at decision time
    liquidity    REAL,
    trade_id     INTEGER
);

CREATE TABLE IF NOT EXISTS trades (
    id             INTEGER PRIMARY KEY,
    pair_address   TEXT NOT NULL,
    name           TEXT,
    wallet_run_id  INTEGER,
    position_id    INTEGER,          -- wallet_positions.position_id
    status         TEXT NOT NULL,    -- pending_entry | open | pending_exit | closed | failed_sell | entry_failed
    entry_reason   TEXT,
    notional       REAL,
    entry_signal_t REAL,
    entry_fill_t   REAL,
    entry_delay_s  REAL,
    entry_mid      REAL,
    entry_price    REAL,             -- effective fill price incl. constant-product impact
    entry_impact   REAL,
    entry_liquidity REAL,
    honeypot       INTEGER,
    exit_reason    TEXT,
    exit_signal_t  REAL,
    exit_fill_t    REAL,
    exit_delay_s   REAL,
    exit_mid       REAL,
    exit_price     REAL,
    exit_impact    REAL,
    fees           REAL,             -- DEX fees, both legs
    tips           REAL,
    proceeds       REAL,
    pnl            REAL,             -- after every cost
    last_obs_t     REAL,             -- newest observation the monitor has looked at
    note           TEXT
);
CREATE INDEX IF NOT EXISTS trades_status ON trades(status);

-- Fast on-chain reads for pairs the executor holds (watch.py). Kept apart from
-- pairs_seen so filters, research and the study never see these partial rows.
CREATE TABLE IF NOT EXISTS pool_reads (
    pair_address   TEXT PRIMARY KEY,
    supported      INTEGER NOT NULL,  -- 1 = read on-chain; 0 = use the 30 s provider readings
    validated      INTEGER NOT NULL DEFAULT 0,  -- first on-chain price agreed with DexScreener
    reason         TEXT,
    base_mint      TEXT,
    quote_mint     TEXT,
    base_vault     TEXT,
    quote_vault    TEXT,
    base_decimals  INTEGER,
    quote_decimals INTEGER,
    checked_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS position_marks (
    id             INTEGER PRIMARY KEY,
    pair_address   TEXT NOT NULL,
    observed_at    TEXT NOT NULL,
    slot           INTEGER,
    price_native   REAL,              -- quote tokens per base token, from the pool's reserves
    quote_usd      REAL,              -- quote token's USD price from the latest provider reading
    price_usd      REAL,
    liquidity_usd  REAL,              -- 2 x quote reserve in USD (DexScreener's definition)
    base_reserve   REAL,
    quote_reserve  REAL
);
CREATE INDEX IF NOT EXISTS position_marks_pair ON position_marks(pair_address, id);

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
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sell_quotes)")}
    for name, decl in [("ref_usd_in", "REAL"), ("ref_amount_in", "INTEGER"),
                       ("ref_out_amount", "INTEGER"), ("measured_impact", "REAL"),
                       ("impact_disagrees", "INTEGER NOT NULL DEFAULT 0")]:
        if name not in cols:
            conn.execute(f"ALTER TABLE sell_quotes ADD COLUMN {name} {decl}")


def event(conn: sqlite3.Connection, kind: str, message: str, data: dict | None = None):
    conn.execute("INSERT INTO events (ts, kind, message, data) VALUES (?, ?, ?, ?)",
                 (now_iso(), kind, message, json.dumps(data) if data else None))
