"""Study tables. Features are append-only; outcomes live in separate tables."""

from __future__ import annotations

SCHEMA = """
CREATE TABLE IF NOT EXISTS study_runs (
    run_id     INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    seed       TEXT NOT NULL,          -- sampling seed for launches added in this run
    config     TEXT NOT NULL           -- JSON snapshot of the study config
);

CREATE TABLE IF NOT EXISTS launch_cohort (
    pair_address     TEXT PRIMARY KEY,
    mint             TEXT,
    source           TEXT NOT NULL,     -- pumpfun_curve | other_curve | pool
    dex              TEXT,
    first_seen_at    TEXT NOT NULL,
    pair_created_at  TEXT,
    run_id           INTEGER NOT NULL REFERENCES study_runs(run_id),
    status           TEXT NOT NULL,     -- launch | excluded_not_new | excluded_no_created_at
    prior_curve_pair TEXT,              -- a pool for a mint first seen on a curve (graduated)
    sample_u         REAL NOT NULL,     -- hash(seed, pair) in [0, 1)
    sample_rate      REAL NOT NULL,
    sampled          INTEGER NOT NULL,
    sampled_reason   TEXT,              -- random | graduate
    added_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS launch_cohort_mint ON launch_cohort(mint);

CREATE TABLE IF NOT EXISTS launch_features (
    id                   INTEGER PRIMARY KEY,
    pair_address         TEXT NOT NULL REFERENCES launch_cohort(pair_address),
    window_min           INTEGER NOT NULL,
    data_cutoff          TEXT NOT NULL,  -- no data after this was used; <= first_seen + window
    captured_at          TEXT NOT NULL,
    n_obs                INTEGER,
    liquidity_usd        REAL,
    price_usd            REAL,
    price_change         REAL,           -- vs first observation, fraction
    vol_m5               REAL,
    vol_h1               REAL,
    buys_h1              INTEGER,
    sells_h1             INTEGER,
    buy_sell_ratio       REAL,
    graduated            INTEGER,
    minutes_to_graduate  REAL,
    curve_read_at        TEXT,
    curve_complete       INTEGER,
    curve_price_sol      REAL,
    curve_virtual_sol    REAL,
    curve_virtual_tokens REAL,
    curve_real_tokens    REAL,
    curve_real_sol       REAL,
    curve_consistent     INTEGER,
    unique_wallets       INTEGER,        -- phase 2b
    trades_per_wallet    REAL,           -- phase 2b
    wash_score           REAL,           -- phase 2b (main Phase 3 wash filter)
    top_holder_share     REAL,           -- needs an RPC key
    bundled_buys         INTEGER,        -- phase 2b
    creator_prior        INTEGER,        -- needs an RPC key
    creator_death_rate   REAL,           -- needs an RPC key
    creator_funding      TEXT,           -- needs an RPC key
    has_website          INTEGER,        -- phase 2b
    has_twitter          INTEGER,        -- phase 2b
    has_telegram         INTEGER,        -- phase 2b
    missing              TEXT NOT NULL,  -- JSON: feature -> why it is NULL
    UNIQUE (pair_address, window_min)
);

CREATE TABLE IF NOT EXISTS launch_outcomes (
    pair_address  TEXT NOT NULL REFERENCES launch_cohort(pair_address),
    checkpoint    TEXT NOT NULL,         -- 1h | 6h | 24h | 72h | 7d
    captured_at   TEXT NOT NULL,
    liquidity_usd REAL,
    volume_h24    REAL,
    price_usd     REAL,
    peak_multiple REAL,                  -- observed peak so far / first price
    label         TEXT NOT NULL,         -- dead | flopped | survived | graduated | unknown
    PRIMARY KEY (pair_address, checkpoint)
);

CREATE TABLE IF NOT EXISTS late_buyer_sims (
    id           INTEGER PRIMARY KEY,
    pair_address TEXT NOT NULL REFERENCES launch_cohort(pair_address),
    entry_min    INTEGER NOT NULL,
    checkpoint   TEXT NOT NULL,
    net_result   REAL,
    exitable     INTEGER,
    details      TEXT
);

CREATE TABLE IF NOT EXISTS study_hypotheses (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    description TEXT NOT NULL,
    window_a    TEXT,
    window_b    TEXT,
    n_a         INTEGER,
    n_b         INTEGER,
    effect      REAL,
    verdict     TEXT
);

CREATE TABLE IF NOT EXISTS study_queue (
    id           INTEGER PRIMARY KEY,
    pair_address TEXT NOT NULL,
    task         TEXT NOT NULL,          -- features (phase 2); checkpoints in phase 3
    window_min   INTEGER NOT NULL,
    due_at       REAL NOT NULL,          -- unix seconds
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | done
    attempts     INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT,
    note         TEXT,
    UNIQUE (pair_address, task, window_min)
);
CREATE INDEX IF NOT EXISTS study_queue_due ON study_queue(status, due_at);

CREATE TABLE IF NOT EXISTS study_gaps (
    id           INTEGER PRIMARY KEY,
    pair_address TEXT NOT NULL,
    task         TEXT NOT NULL,
    window_min   INTEGER,
    ts           TEXT NOT NULL,
    reason       TEXT NOT NULL
);

-- Features are frozen the moment they are captured.
CREATE TRIGGER IF NOT EXISTS launch_features_no_update BEFORE UPDATE ON launch_features
BEGIN SELECT RAISE(ABORT, 'launch_features are append-only'); END;
CREATE TRIGGER IF NOT EXISTS launch_features_no_delete BEFORE DELETE ON launch_features
BEGIN SELECT RAISE(ABORT, 'launch_features are append-only'); END;

-- Cohort membership is permanent; only "sampled" may change, and only 0 -> 1.
CREATE TRIGGER IF NOT EXISTS launch_cohort_no_delete BEFORE DELETE ON launch_cohort
BEGIN SELECT RAISE(ABORT, 'cohort membership is permanent'); END;
CREATE TRIGGER IF NOT EXISTS launch_cohort_fixed BEFORE UPDATE OF
    pair_address, mint, source, dex, first_seen_at, pair_created_at, run_id, status,
    prior_curve_pair, sample_u, sample_rate, added_at ON launch_cohort
BEGIN SELECT RAISE(ABORT, 'cohort rows are immutable except sampling upgrades'); END;
CREATE TRIGGER IF NOT EXISTS launch_cohort_sampled_only_up BEFORE UPDATE OF sampled ON launch_cohort
WHEN NEW.sampled < OLD.sampled
BEGIN SELECT RAISE(ABORT, 'a sampled launch cannot be unsampled'); END;

CREATE TRIGGER IF NOT EXISTS study_gaps_no_update BEFORE UPDATE ON study_gaps
BEGIN SELECT RAISE(ABORT, 'study_gaps is append-only'); END;
CREATE TRIGGER IF NOT EXISTS study_gaps_no_delete BEFORE DELETE ON study_gaps
BEGIN SELECT RAISE(ABORT, 'study_gaps is append-only'); END;
"""


def create(conn) -> None:
    conn.executescript(SCHEMA)
