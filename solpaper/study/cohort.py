"""Cohort capture and sampling.

Every pair the scanner records joins the cohort once. Only launches (created
shortly before we first saw them) get feature tasks. A random share is sampled
for later wallet-level data. The draw is a hash of the run's seed and the pair
address, made when the pair joins, before any filter result exists, so
sampling can't depend on how a token looks.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone

from ..config import dex_label
from ..db import now_iso


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso(dt: datetime) -> str:
    """Same format as db.now_iso, so ISO strings compare correctly."""
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def source_for(cfg: dict, dex: str | None) -> str:
    return cfg["study"]["sources"].get(dex_label(cfg, dex), "pool")


def sample_u(seed: str, pair: str) -> float:
    h = hashlib.sha256(f"{seed}|{pair}".encode()).digest()
    return int.from_bytes(h[:8], "big") / 2 ** 64


def start_run(conn, cfg: dict, seed: str | None = None) -> tuple[int, str]:
    seed = seed or secrets.token_hex(16)
    cur = conn.execute("INSERT INTO study_runs (started_at, seed, config) VALUES (?, ?, ?)",
                       (now_iso(), seed, json.dumps(cfg["study"], sort_keys=True)))
    return cur.lastrowid, seed


def mark_graduate(conn, pair: str) -> None:
    """Graduates are always sampled (spec SF-04)."""
    conn.execute("UPDATE launch_cohort SET sampled = 1, sampled_reason = 'graduate' "
                 "WHERE pair_address = ? AND sampled = 0", (pair,))


def sync(conn, cfg: dict, run_id: int, seed: str) -> int:
    """Add pairs the scanner has recorded since the last sync. Returns how many."""
    sc = cfg["study"]
    rows = conn.execute("""
        SELECT p.* FROM pairs p
        LEFT JOIN launch_cohort c ON c.pair_address = p.pair_address
        WHERE c.pair_address IS NULL ORDER BY p.first_seen_at""").fetchall()
    for p in rows:
        source = source_for(cfg, p["dex"])
        first_seen = parse_ts(p["first_seen_at"])
        created = parse_ts(p["pair_created_at"])
        if created is None:
            status = "excluded_no_created_at"
        elif first_seen - created > timedelta(minutes=sc["max_launch_age_min"]):
            status = "excluded_not_new"
        else:
            status = "launch"
        prior = None
        if source == "pool" and p["base_mint"]:
            r = conn.execute("SELECT pair_address FROM launch_cohort WHERE mint = ? AND "
                             "source != 'pool' LIMIT 1", (p["base_mint"],)).fetchone()
            if r:
                prior = r["pair_address"]
                mark_graduate(conn, prior)
        u = sample_u(seed, p["pair_address"])
        sampled = status == "launch" and u < sc["sample_rate"]
        conn.execute(
            "INSERT INTO launch_cohort (pair_address, mint, source, dex, first_seen_at, "
            "pair_created_at, run_id, status, prior_curve_pair, sample_u, sample_rate, sampled, "
            "sampled_reason, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (p["pair_address"], p["base_mint"], source, dex_label(cfg, p["dex"]),
             p["first_seen_at"], p["pair_created_at"], run_id, status, prior, u,
             sc["sample_rate"], int(sampled), "random" if sampled else None, now_iso()))
        if status == "launch":
            for w in sc["windows_min"]:
                due = first_seen.timestamp() + w * 60 - sc["lead_s"]
                conn.execute("INSERT OR IGNORE INTO study_queue (pair_address, task, window_min, "
                             "due_at, updated_at) VALUES (?, 'features', ?, ?, ?)",
                             (p["pair_address"], w, due, now_iso()))
    return len(rows)
