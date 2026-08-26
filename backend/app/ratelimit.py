"""In-memory sliding-window rate limiting.

This exists to protect the Google Maps bill, not to stop a determined attacker.
A public URL puts every search on the owner's card, and at roughly $0.41 a cold
search a simple loop could drain a month's free tier in minutes.

Two layers:

* **Per-client limits** keep one visitor from hammering the expensive endpoint.
* **A global daily ceiling** is the actual backstop. Per-client limits do nothing
  against many clients (or one client with many addresses), so the global cap is
  what bounds the worst case.

State is per-process and resets on restart, which is fine for a single-worker
deployment. The authoritative backstop remains a hard quota cap set in the
Google Cloud console -- this is the polite first line, not the last one.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Rule:
    limit: int
    window_seconds: int

    def describe(self) -> str:
        if self.window_seconds >= 86_400:
            unit = "day"
        elif self.window_seconds >= 3600:
            unit = "hour"
        elif self.window_seconds >= 60:
            unit = "minute"
        else:
            unit = f"{self.window_seconds}s"
        return f"{self.limit} per {unit}"


class SlidingWindowLimiter:
    """Tracks hit timestamps per key and answers "is one more allowed?"."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, cutoff: float) -> deque[float]:
        hits = self._hits[key]
        while hits and hits[0] <= cutoff:
            hits.popleft()
        return hits

    def check(self, key: str, rule: Rule, now: Optional[float] = None) -> Optional[int]:
        """Record a hit if allowed. Returns None when allowed, otherwise the
        number of seconds to wait before retrying."""
        now = time.time() if now is None else now
        with self._lock:
            hits = self._prune(key, now - rule.window_seconds)
            if len(hits) >= rule.limit:
                retry_after = rule.window_seconds - (now - hits[0])
                return max(1, int(retry_after) + 1)
            hits.append(now)
            return None

    def peek(self, key: str, rule: Rule, now: Optional[float] = None) -> int:
        """Hits used in the current window, without recording one."""
        now = time.time() if now is None else now
        with self._lock:
            return len(self._prune(key, now - rule.window_seconds))

    def forget(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def sweep(self, max_window: int, now: Optional[float] = None) -> int:
        """Drop keys with no recent activity so memory can't grow unbounded."""
        now = time.time() if now is None else now
        with self._lock:
            stale = [
                key
                for key, hits in self._hits.items()
                if not hits or hits[-1] <= now - max_window
            ]
            for key in stale:
                del self._hits[key]
            return len(stale)


GLOBAL_KEY = "__all__"


class RateLimitError(Exception):
    """Raised when a request should be rejected with 429."""

    def __init__(self, retry_after: int, message: str) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.message = message


def client_key(forwarded_for: Optional[str], client_host: Optional[str], trust_proxy: bool) -> str:
    """Identify the caller.

    Behind a proxy the socket address is the proxy's, so the real client is the
    first entry of X-Forwarded-For. That header is trivially spoofed when the app
    is exposed directly, so it is only honoured when explicitly enabled.
    """
    if trust_proxy and forwarded_for:
        first = forwarded_for.split(",")[0].strip()
        if first:
            return first
    return client_host or "unknown"
