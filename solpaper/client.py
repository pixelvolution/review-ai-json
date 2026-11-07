"""Cached, budgeted HTTP GET for read-only market-data APIs.

Order of operations for every call:
  1. fresh cache entry?  -> return it, no request sent;
  2. provider in backoff? -> return None (the gap is already being logged);
  3. pace the request inside the provider bucket's budget;
  4. send, log it in request_log, then cache it or start/extend a data gap.

On any failure it returns None. Callers must treat None as "no data" and
never invent a value in its place.

This client only does GET. There is no POST helper, and nothing here can
sign or send a transaction. Phase 2's Solana RPC reads use a separate,
allow-listed read-only call.
"""

from __future__ import annotations

import json
import logging
import time
from urllib.parse import urlencode

import httpx

from .budget import Backoff, Pacer
from .db import event, now_iso

log = logging.getLogger("solpaper.http")
RETRYABLE = {429, 500, 502, 503, 504}


class Http:
    def __init__(self, conn, cfg: dict, transport: httpx.BaseTransport | None = None,
                 clock=time.monotonic, wall=time.time, sleep=time.sleep):
        self.conn, self.cfg = conn, cfg
        self.wall = wall
        self.clients = {}
        self.pacers: dict[tuple[str, str], Pacer] = {}
        self.backoffs: dict[str, Backoff] = {}
        self.gap_id: dict[str, int] = {}
        self.last_age_s: float | None = None  # data age of the last returned response
        self.last_from_cache = False          # was it served from our own cache?
        b = cfg.get("backoff", {})
        for name, p in cfg["providers"].items():
            self.clients[name] = httpx.Client(
                base_url=p["base_url"], timeout=p.get("timeout_s", 20), transport=transport,
                headers={"accept": "application/json",
                         "user-agent": "solpaper-research/0.1 (paper trading simulator)"})
            for bucket, lim in p["buckets"].items():
                self.pacers[(name, bucket)] = Pacer(lim["rpm"], lim.get("safety", 1.0),
                                                    clock=clock, sleep=sleep)
            self.backoffs[name] = Backoff(b.get("base_s", 2), b.get("max_s", 300),
                                          b.get("jitter", 0.2), clock=clock)

    # ------------------------------------------------------------------ cache
    def _cache_get(self, key: str, fresh_s: float):
        row = self.conn.execute("SELECT fetched_at, cdn_age_s, body FROM http_cache "
                                "WHERE key = ?", (key,)).fetchone()
        if row and self.wall() - row["fetched_at"] < fresh_s:
            self.last_age_s = self.wall() - row["fetched_at"] + (row["cdn_age_s"] or 0)
            self.last_from_cache = True
            return json.loads(row["body"])
        return None

    def _cache_put(self, key, provider, url, status, body, cdn_age):
        self.conn.execute(
            "INSERT OR REPLACE INTO http_cache (key, provider, url, fetched_at, status, "
            "cdn_age_s, body) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key, provider, url, self.wall(), status, cdn_age, body))

    # ------------------------------------------------------------------ gaps
    def _gap_failure(self, provider, status, error):
        gid = self.gap_id.get(provider)
        if gid is None:
            cur = self.conn.execute(
                "INSERT INTO data_gaps (provider, started_at, attempts, last_status, last_error)"
                " VALUES (?, ?, 1, ?, ?)", (provider, now_iso(), status, error))
            self.gap_id[provider] = cur.lastrowid
            event(self.conn, "data_gap", f"{provider}: data gap started ({status or error})")
        else:
            self.conn.execute("UPDATE data_gaps SET attempts = attempts + 1, last_status = ?, "
                              "last_error = ? WHERE id = ?", (status, error, gid))

    def _gap_success(self, provider):
        gid = self.gap_id.pop(provider, None)
        if gid is not None:
            self.conn.execute("UPDATE data_gaps SET ended_at = ? WHERE id = ?", (now_iso(), gid))
            row = self.conn.execute("SELECT started_at, attempts FROM data_gaps WHERE id = ?",
                                    (gid,)).fetchone()
            event(self.conn, "data_gap_end",
                  f"{provider}: data gap ended after {row['attempts']} failed attempts "
                  f"(since {row['started_at']})")

    # ------------------------------------------------------------------ get
    def get_json(self, provider: str, bucket: str, path: str, params: dict | None = None,
                 fresh_s: float = 0):
        """GET ``path`` from ``provider``. Returns parsed JSON or None."""
        q = ("?" + urlencode(sorted(params.items()))) if params else ""
        key = f"{provider}|{path}{q}"
        if fresh_s > 0:
            hit = self._cache_get(key, fresh_s)
            if hit is not None:
                return hit
        bo = self.backoffs[provider]
        if bo.blocked:
            return None
        self.pacers[(provider, bucket)].wait()
        url = f"{self.cfg['providers'][provider]['base_url']}{path}{q}"
        t0 = time.monotonic()
        status = body = err = None
        headers = {}
        try:
            r = self.clients[provider].get(path, params=params)
            status, body, headers = r.status_code, r.text, r.headers
        except httpx.HTTPError as e:
            err = f"{type(e).__name__}: {e}"
        ms = (time.monotonic() - t0) * 1000
        limits = {k: v for k, v in headers.items() if "ratelimit" in k.lower()
                  or k.lower() == "retry-after"}
        try:
            age = float(headers.get("age")) if headers.get("age") else None
        except ValueError:
            age = None
        self.conn.execute(
            "INSERT INTO request_log (ts, provider, bucket, url, status, ms, bytes, age_s, "
            "error, limits) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now_iso(), provider, bucket, url, status, round(ms, 1),
             len(body) if body else 0, age, err,
             json.dumps(limits) if limits else None))

        if status == 200:
            try:
                data = json.loads(body)
            except json.JSONDecodeError as e:
                err = f"bad JSON: {e}"
            else:
                self._cache_put(key, provider, url, status, body, age)
                self.last_age_s = age or 0.0
                self.last_from_cache = False
                bo.success()
                self._gap_success(provider)
                return data
        if status in RETRYABLE or err:
            ra = headers.get("retry-after")
            try:
                ra = float(ra) if ra else None
            except ValueError:
                ra = None
            delay = bo.failure(ra)
            self._gap_failure(provider, status, err)
            log.warning("%s %s -> %s; backing off %.0fs", provider, path, status or err, delay)
        else:
            event(self.conn, "http_error", f"{provider} {path} -> {status}",
                  {"url": url, "status": status})
        return None
