"""Decides how long to wait between requests. Pure logic, no sleeping: the worker does the waiting.

Rules:
- One request at a time. Base delay (default 1.0 s, never below the floor) with random jitter.
- A longer break (~60 s) after every N requests (default 500).
- If the site gets slow (rolling average response time above `slow_latency`), the delay
  doubles; above twice that, it quadruples.
- On a transient failure (HTTP error, 429/403, timeout, "blocked" page) the backoff grows
  exponentially: 30 s, 60 s, 120 s ... capped at 15 min. A Retry-After header is honoured.
- After `max_consecutive_failures` failures in a row, `tripped` becomes True and the worker
  auto-pauses until a human presses Resume.
"""
from __future__ import annotations

import random
from collections import deque


class Pacer:
    def __init__(
        self,
        base_delay: float = 1.0,
        jitter: float = 0.3,              # +/- fraction of the delay
        floor: float = 0.75,
        break_every: int = 500,
        break_seconds: float = 60.0,
        backoff_start: float = 30.0,
        backoff_cap: float = 900.0,
        retry_after_cap: float = 3600.0,
        slow_latency: float = 3.0,
        latency_window: int = 20,
        max_consecutive_failures: int = 5,
        rng: random.Random | None = None,
    ):
        self.floor = floor
        self.base_delay = max(floor, base_delay)
        self.jitter = jitter
        self.break_every = break_every
        self.break_seconds = break_seconds
        self.backoff_start = backoff_start
        self.backoff_cap = backoff_cap
        self.retry_after_cap = retry_after_cap
        self.slow_latency = slow_latency
        self.max_consecutive_failures = max_consecutive_failures
        self.rng = rng or random.Random()

        self.requests_sent = 0
        self.consecutive_failures = 0
        self._latencies: deque[float] = deque(maxlen=latency_window)
        self._last_break_at = 0

    # ---- state -------------------------------------------------------------
    @property
    def avg_latency(self) -> float:
        return sum(self._latencies) / len(self._latencies) if self._latencies else 0.0

    @property
    def slowdown(self) -> int:
        """1 normally, 2 when the site is slow, 4 when it is very slow."""
        if len(self._latencies) < 5:
            return 1
        avg = self.avg_latency
        if avg > 2 * self.slow_latency:
            return 4
        if avg > self.slow_latency:
            return 2
        return 1

    @property
    def tripped(self) -> bool:
        return self.consecutive_failures >= self.max_consecutive_failures

    # ---- decisions ---------------------------------------------------------
    def next_delay(self) -> tuple[float, str | None]:
        """Seconds to wait before the next request, plus a reason if it's longer than usual."""
        reason = None
        d = self.base_delay * self.slowdown
        if self.slowdown > 1:
            reason = f"site is slow (avg {self.avg_latency:.1f}s), delay x{self.slowdown}"
        d *= 1 + self.rng.uniform(-self.jitter, self.jitter)
        d = max(self.floor, d)
        if (self.break_every and self.requests_sent > 0
                and self.requests_sent % self.break_every == 0
                and self._last_break_at != self.requests_sent):
            self._last_break_at = self.requests_sent
            pause = self.break_seconds * self.rng.uniform(0.8, 1.2)
            d += pause
            reason = f"scheduled break after {self.requests_sent:,} requests ({pause:.0f}s)"
        return d, reason

    def record_success(self, latency: float) -> None:
        self.requests_sent += 1
        self.consecutive_failures = 0
        self._latencies.append(max(0.0, latency))

    def record_failure(self, retry_after: float | None = None) -> float:
        """Register a transient failure and return how long to back off."""
        self.requests_sent += 1
        self.consecutive_failures += 1
        backoff = min(self.backoff_cap, self.backoff_start * 2 ** (self.consecutive_failures - 1))
        if retry_after is not None:
            backoff = max(backoff, min(retry_after, self.retry_after_cap))
        return backoff

    def reset_failures(self) -> None:
        self.consecutive_failures = 0
