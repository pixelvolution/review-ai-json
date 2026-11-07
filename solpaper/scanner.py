"""Scanner: discover new / spiking Solana pairs and log every observation.

    python -m solpaper.scanner --minutes 15     # run for a while
    python -m solpaper.scanner                  # run until stopped

Jobs (intervals in config.yaml):
  gt_new_pools     GeckoTerminal newest pools (the main new-pair feed)
  gt_trending      GeckoTerminal trending pools (volume spikes)
  ds_boosts        DexScreener newly boosted tokens
  ds_profiles      DexScreener newly listed token profiles
  refresh_tracked  re-quote every tracked pair (DexScreener first, 30 per
                   call; GeckoTerminal for pairs DexScreener doesn't index yet)

Every snapshot becomes a pairs_seen row. When a provider fails, the job
logs the gap and writes nothing. No value is ever guessed.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timedelta, timezone

from .client import Http
from .config import load_config
from .db import connect, event, now_iso
from .providers import DexScreener, GeckoTerminal, PairSnapshot

log = logging.getLogger("solpaper.scanner")


def is_volume_spike(s: PairSnapshot, cfg: dict, now: datetime | None = None) -> bool:
    """m5 volume well above the pair's h1 per-5-minute average.

    Only for pairs at least an hour old: for a younger pair the h1 window
    is mostly empty, so every new pair would look like a spike.
    """
    vs = cfg["scanner"]["volume_spike"]
    m5, h1 = s.vol.get("m5"), s.vol.get("h1")
    if m5 is None or h1 is None or m5 < vs["min_m5_usd"] or not s.pair_created_at:
        return False
    created = datetime.fromisoformat(s.pair_created_at.replace("Z", "+00:00"))
    if (now or datetime.now(timezone.utc)) - created < timedelta(hours=1):
        return False
    return m5 >= vs["multiple"] * (h1 / 12)


class Scanner:
    def __init__(self, cfg: dict, conn=None, http: Http | None = None, clock=time.monotonic):
        self.cfg = cfg
        self.conn = conn or connect(cfg["db_path"])
        self.http = http or Http(self.conn, cfg)
        self.gt = GeckoTerminal(self.http, cfg)
        self.ds = DexScreener(self.http, cfg)
        self.clock = clock
        self.next_run: dict[str, float] = {}
        self.stop = False

    # ---------------------------------------------------------------- logging
    def record(self, snaps: list[PairSnapshot]) -> int:
        n = 0
        for s in snaps:
            if not s.pair_address or s.from_cache:
                continue  # a cached response was already recorded when it was fetched
            new = self.conn.execute(
                "INSERT OR IGNORE INTO pairs (pair_address, dex, base_mint, quote_mint, "
                "base_symbol, name, pair_created_at, first_seen_at, first_source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (s.pair_address, s.dex, s.base_mint, s.quote_mint, s.base_symbol, s.name,
                 s.pair_created_at, now_iso(), s.source)).rowcount
            spike = is_volume_spike(s, self.cfg)
            fam = "ds%" if s.source.startswith("ds") else "gt%"
            prev = self.conn.execute(
                "SELECT price_usd, liquidity_usd, vol_m5, vol_h1, buys_h1, sells_h1 "
                "FROM pairs_seen WHERE pair_address = ? AND source LIKE ? "
                "ORDER BY id DESC LIMIT 1", (s.pair_address, fam)).fetchone()
            unchanged = prev is not None and tuple(prev) == (
                s.price_usd, s.liquidity_usd, s.vol.get("m5"), s.vol.get("h1"),
                s.buys.get("h1"), s.sells.get("h1"))
            self.conn.execute(
                "INSERT INTO pairs_seen (pair_address, observed_at, source, data_age_s, "
                "price_usd, price_native, liquidity_usd, fdv_usd, mcap_usd, vol_m5, vol_h1, "
                "vol_h6, vol_h24, buys_m5, sells_m5, buys_h1, sells_h1, buys_h24, sells_h24, "
                "chg_m5, chg_h1, chg_h24, volume_spike, unchanged) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (s.pair_address, now_iso(), s.source, s.data_age_s, s.price_usd,
                 s.price_native, s.liquidity_usd, s.fdv_usd, s.mcap_usd, s.vol.get("m5"),
                 s.vol.get("h1"), s.vol.get("h6"), s.vol.get("h24"), s.buys.get("m5"),
                 s.sells.get("m5"), s.buys.get("h1"), s.sells.get("h1"), s.buys.get("h24"),
                 s.sells.get("h24"), s.change.get("m5"), s.change.get("h1"),
                 s.change.get("h24"), int(spike), int(unchanged)))
            if new:
                event(self.conn, "pair_new", f"new pair {s.name} {s.pair_address} via {s.source}",
                      {"pair": s.pair_address, "created": s.pair_created_at})
            if spike:
                event(self.conn, "volume_spike",
                      f"{s.name} m5 ${s.vol['m5']:,.0f} vs h1 avg ${s.vol['h1'] / 12:,.0f}/5m",
                      {"pair": s.pair_address})
            n += 1
        return n

    # ---------------------------------------------------------------- tracking
    def tracked(self) -> list[str]:
        sc = self.cfg["scanner"]
        since = (datetime.now(timezone.utc) - timedelta(hours=sc["track_hours"])).isoformat()
        rows = self.conn.execute(
            "SELECT pair_address FROM pairs WHERE first_seen_at >= ? "
            "ORDER BY first_seen_at DESC LIMIT ?", (since, sc["max_tracked"])).fetchall()
        return [r["pair_address"] for r in rows]

    def refresh_tracked(self) -> int:
        addrs = self.tracked()
        got: list[PairSnapshot] = []
        seen: set[str] = set()
        for k in range(0, len(addrs), self.ds.max_batch):
            batch = addrs[k:k + self.ds.max_batch]
            snaps = self.ds.pairs(batch)
            if snaps is None:
                continue  # gap already logged by the client
            got.extend(snaps)
            seen.update(s.pair_address for s in snaps)
        missing = [a for a in addrs if a not in seen]
        # DexScreener often lags on brand-new pools: GeckoTerminal fills in, budget permitting.
        for k in range(0, min(len(missing), self.gt.max_batch * 2), self.gt.max_batch):
            snaps = self.gt.pairs(missing[k:k + self.gt.max_batch])
            if snaps is not None:
                got.extend(snaps)
        return self.record(got)

    # ---------------------------------------------------------------- loop
    def run_job(self, job: str) -> int | None:
        if job == "refresh_tracked":
            return self.refresh_tracked()
        if job == "filter":
            from .filters import evaluate_tracked
            return evaluate_tracked(self)
        if job == "trade":
            from .executor import Executor
            if getattr(self, "_executor", None) is None:
                self._executor = Executor(self.conn, self.cfg)
            r = self._executor.tick()
            return r["entries"] + r["fills"] + r["exits"]
        src = self.gt if job.startswith("gt_") else self.ds
        snaps = src.discover(job)
        return None if snaps is None else self.record(snaps)

    def tick(self):
        for job, every in self.cfg["scanner"]["intervals_s"].items():
            start = self.clock()  # each job is timed from its own start, not the pass's
            if start >= self.next_run.get(job, 0):
                self.next_run[job] = start + every
                t0 = time.monotonic()
                try:
                    n = self.run_job(job)
                except Exception as e:  # noqa: BLE001 - one bad job must not stop the loop
                    log.exception("job %s failed", job)
                    event(self.conn, "job_error", f"{job}: {type(e).__name__}: {e}")
                    continue
                log.info("%-16s %s in %.1fs", job,
                         "no data (gap)" if n is None else
                         f"{n} pairs evaluated" if job == "filter" else
                         f"{n} trade actions" if job == "trade" else f"{n} new observations",
                         time.monotonic() - t0)

    def run(self, minutes: float | None = None):
        end = None if minutes is None else self.clock() + minutes * 60
        event(self.conn, "scanner_start", "scanner started", {"minutes": minutes})
        while not self.stop and (end is None or self.clock() < end):
            self.tick()
            time.sleep(self.cfg["scanner"]["tick_s"])
        event(self.conn, "scanner_stop", "scanner stopped")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Scan new Solana pairs (read-only).")
    ap.add_argument("--minutes", type=float)
    ap.add_argument("--config")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    sc = Scanner(load_config(a.config))

    def _stop(*_):
        sc.stop = True
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    sc.run(a.minutes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
