"""Stored price history: the ``pairs_seen`` table that replays read from.

The live bot appends one row per pair per bar it sees. Replays only ever read
from here, and only up to the end of the window they're asked for.

    python market.py import-binance     # seed from research/data/*.csv
    python market.py info
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
DEFAULT_MARKET_DB = HERE / "market.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS pairs_seen (
    pair   TEXT NOT NULL,
    ts     TEXT NOT NULL,      -- bar open time, UTC ISO-8601
    open   REAL NOT NULL,
    high   REAL NOT NULL,
    low    REAL NOT NULL,
    close  REAL NOT NULL,
    volume REAL NOT NULL DEFAULT 0,  -- quote volume
    PRIMARY KEY (pair, ts)
);
"""


def market_db_path(path=None) -> Path:
    return Path(path or os.environ.get("MOCK_WALLET_MARKET_DB") or DEFAULT_MARKET_DB)


def connect(path=None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(market_db_path(path)))
    conn.executescript(SCHEMA)
    return conn


def _iso(ts) -> str:
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    return t.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def record_bars(df: pd.DataFrame, pair: str, path=None) -> int:
    """Append bars (columns t, open, high, low, close, [quote_volume|volume])."""
    vol = df["quote_volume"] if "quote_volume" in df else df.get("volume", 0)
    rows = [(pair, _iso(t), float(o), float(h), float(l), float(c), float(v))
            for t, o, h, l, c, v in zip(df["t"], df["open"], df["high"], df["low"],
                                        df["close"], vol)]
    conn = connect(path)
    with conn:
        conn.executemany("INSERT OR IGNORE INTO pairs_seen VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    conn.close()
    return len(rows)


def pairs(path=None) -> list[str]:
    conn = connect(path)
    try:
        return [r[0] for r in conn.execute("SELECT DISTINCT pair FROM pairs_seen ORDER BY pair")]
    finally:
        conn.close()


def load_bars(pair_list, start, end, path=None) -> dict[str, pd.DataFrame]:
    """Bars with start <= ts < end. Nothing at or after ``end`` is ever read."""
    conn = connect(path)
    try:
        out = {}
        for pr in pair_list:
            df = pd.read_sql_query(
                "SELECT ts, open, high, low, close, volume FROM pairs_seen "
                "WHERE pair = ? AND ts >= ? AND ts < ? ORDER BY ts",
                conn, params=(pr, _iso(start), _iso(end)))
            if len(df):
                df["ts"] = pd.to_datetime(df["ts"], utc=True)
                out[pr] = df.set_index("ts")
        return out
    finally:
        conn.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["import-binance", "info"])
    ap.add_argument("--db")
    a = ap.parse_args(argv)
    if a.cmd == "import-binance":
        sys.path.insert(0, str(HERE / "research"))
        from data import DATA_DIR, load  # noqa: E402
        for f in sorted(DATA_DIR.glob("*_1h.csv")):
            sym = f.name.split("_")[0]
            print(sym, record_bars(load(sym), sym, a.db))
    else:
        conn = connect(a.db)
        for r in conn.execute("SELECT pair, COUNT(*), MIN(ts), MAX(ts) FROM pairs_seen "
                              "GROUP BY pair ORDER BY pair"):
            print(*r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
