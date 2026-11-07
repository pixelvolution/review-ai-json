"""Request budget: per-provider pacing and exponential backoff.

* Pacing: each bucket allows ``rpm * safety`` requests per minute, spaced
  evenly (one every 60 / (rpm * safety) seconds). The scheduler slows down
  long before the provider's cap instead of bursting into it.
* Backoff: after a 429, a 5xx or a network error the provider is paused for
  base * 2^(n-1) seconds (capped, jittered); a Retry-After header wins if
  it asks for longer. While paused, no requests are sent.
"""

from __future__ import annotations

import random
import time


class Pacer:
    def __init__(self, rpm: float, safety: float = 1.0, clock=time.monotonic, sleep=time.sleep):
        allowed = rpm * safety
        if allowed <= 0:
            raise ValueError("rpm * safety must be > 0")
        self.interval = 60.0 / allowed
        self.clock, self.sleep = clock, sleep
        self.next_at = 0.0

    def wait(self) -> float:
        """Block until the next request may go out. Returns seconds waited."""
        now = self.clock()
        waited = max(0.0, self.next_at - now)
        if waited:
            self.sleep(waited)
        self.next_at = max(now, self.next_at) + self.interval
        return waited


class Backoff:
    def __init__(self, base_s=2.0, max_s=300.0, jitter=0.2, clock=time.monotonic,
                 rng: random.Random | None = None):
        self.base, self.max, self.jitter = base_s, max_s, jitter
        self.clock = clock
        self.rng = rng or random.Random()
        self.failures = 0
        self.until = 0.0

    @property
    def blocked(self) -> bool:
        return self.clock() < self.until

    def remaining(self) -> float:
        return max(0.0, self.until - self.clock())

    def failure(self, retry_after: float | None = None) -> float:
        self.failures += 1
        delay = min(self.base * 2 ** (self.failures - 1), self.max)
        delay *= 1 + self.rng.uniform(-self.jitter, self.jitter)
        if retry_after:
            delay = max(delay, retry_after)
        self.until = self.clock() + delay
        return delay

    def success(self) -> int:
        """Reset; returns how many consecutive failures preceded this success."""
        n, self.failures, self.until = self.failures, 0, 0.0
        return n
