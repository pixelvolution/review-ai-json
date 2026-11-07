"""Download and cache historical candles.

    python data.py                 # 2 years of 1h candles for the default basket
    python data.py --interval 15m --days 180

Source: Binance public market data (data-api.binance.vision), no key needed.
Cached as CSV under research/data/ (gitignored).
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent / "data"
API = "https://data-api.binance.vision/api/v3/klines"

MEMES = ["DOGE", "SHIB", "PEPE", "FLOKI", "BONK", "WIF", "BOME", "MEME", "PEOPLE",
         "1000SATS", "NEIRO", "TURBO", "PNUT", "ACT", "TRUMP", "PENGU"]
MAJORS = ["BTC", "ETH", "SOL"]

_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000,
       "1d": 86_400_000}


def _get(url: str, tries: int = 5):
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.loads(r.read())
        except Exception:  # noqa: BLE001
            if i == tries - 1:
                raise
            time.sleep(2 ** i)


def fetch(symbol: str, interval: str = "1h", days: int = 730, end: datetime | None = None
          ) -> pd.DataFrame:
    end = end or datetime.now(timezone.utc)
    start_ms = int((end - timedelta(days=days)).timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    rows = []
    cur = start_ms
    while cur < end_ms:
        batch = _get(f"{API}?symbol={symbol}USDT&interval={interval}"
                     f"&startTime={cur}&endTime={end_ms}&limit=1000")
        if not batch:
            break
        rows.extend(batch)
        cur = batch[-1][0] + _MS[interval]
        if len(batch) < 1000:
            break
    df = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close", "volume", "ct",
                                     "qv", "n", "tb", "tq", "x"])
    df = df[["t", "open", "high", "low", "close", "qv"]].rename(columns={"qv": "quote_volume"})
    for c in ["open", "high", "low", "close", "quote_volume"]:
        df[c] = df[c].astype(float)
    df["t"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    return df.drop_duplicates("t").reset_index(drop=True)


def path_for(symbol: str, interval: str) -> Path:
    return DATA_DIR / f"{symbol}_{interval}.csv"


def load(symbol: str, interval: str = "1h") -> pd.DataFrame:
    df = pd.read_csv(path_for(symbol, interval))
    df["t"] = pd.to_datetime(df["t"], utc=True)
    return df


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--interval", default="1h")
    p.add_argument("--days", type=int, default=730)
    p.add_argument("--symbols", nargs="*", default=MEMES + MAJORS)
    a = p.parse_args()
    DATA_DIR.mkdir(exist_ok=True)
    for s in a.symbols:
        df = fetch(s, a.interval, a.days)
        df.to_csv(path_for(s, a.interval), index=False)
        print(f"{s:9s} {len(df):6d} bars  {df.t.iloc[0]:%Y-%m-%d} -> {df.t.iloc[-1]:%Y-%m-%d}")


if __name__ == "__main__":
    main()
