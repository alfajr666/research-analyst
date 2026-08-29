"""Base classes for rate-limited external API clients.

Follows specs/external-api-rate-limiting.md exactly.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import httpx

import config


@dataclass
class RateLimitInfo:
    retry_after: float = 5.0
    remaining: Optional[int] = None
    reset_at: Optional[float] = None


class TokenBucket:
    """Simple token bucket rate limiter (proactive)."""

    def __init__(self, rate_per_sec: float, capacity: int = 10):
        self.rate = rate_per_sec
        self.capacity = capacity
        self.tokens = float(capacity)
        self.last_refill = time.time()

    def acquire(self, tokens: float = 1.0, timeout: float = 30.0) -> bool:
        start = time.time()
        while True:
            self._refill()
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            if time.time() - start > timeout:
                return False
            try:
                time.sleep(0.05)
            except KeyboardInterrupt:
                print("backoff interrupted", flush=True)
                return False

    def _refill(self):
        now = time.time()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last_refill = now


class RateLimitedClient:
    """Base client that enforces rate limits, deadlines, logging, and unavailable semantics."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        source_name: str,
        rate_per_sec: float = 0.08,
        max_concurrent: int = 5,
        default_timeout: float = 15.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.source_name = source_name
        self.auth_in_query = True  # override in subclass if header only
        self.bucket = TokenBucket(rate_per_sec)
        self.semaphore = None  # set in subclasses if using async
        self.default_timeout = default_timeout
        self._client = httpx.Client(timeout=default_timeout)

    def _log_request(
        self,
        cutoff_id: str,
        request_type: str,
        weight: int,
        status: str,
        meta: Dict[str, Any],
        db_path: Optional[str] = None,
    ) -> None:
        """Logs to source_request_log (shared with OpenMarket)."""
        conn = config.get_db_connection(db_path=db_path)
        try:
            conn.execute(
                """
                INSERT INTO source_request_log (
                    request_id, cutoff_id, source, request_type, weight,
                    budget_remaining, selected_universe_json, status,
                    requested_at, completed_at, response_meta_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"{self.source_name}-{cutoff_id}-{request_type}-{int(time.time()*1000)}-{uuid.uuid4().hex[:8]}",
                    cutoff_id,
                    self.source_name,
                    request_type,
                    weight,
                    meta.get("budget_remaining"),
                    "[]",
                    status,
                    datetime.now(timezone.utc),
                    datetime.now(timezone.utc),
                    str(meta)[:500],
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _get_headers(self) -> Dict[str, str]:
        return {"Accept": "application/json"}

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict] = None,
        cutoff_id: str = "no-cutoff",
        request_type: str = "unknown",
        weight: int = 1,
        timeout: Optional[float] = None,
        db_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Core request with rate limiting, logging, and error classification."""
        if not self.api_key:
            self._log_request(cutoff_id, request_type, 0, "skipped", {"reason": "no_key"}, db_path=db_path)
            return {"status": "unavailable", "reason": "no_api_key"}

        # Proactive rate limit
        if not self.bucket.acquire(weight):
            self._log_request(cutoff_id, request_type, weight, "rate_limited_pre", {}, db_path=db_path)
            return {"status": "unavailable", "reason": "preemptive_rate_limit"}

        url = f"{self.base_url}/{path.lstrip('/')}"
        qparams = dict(params or {})
        if self.api_key and self.auth_in_query and "api_key" not in qparams:
            qparams["api_key"] = self.api_key  # CoinAnalyze style; OpenMarket may differ

        start = time.time()
        try:
            resp = self._client.request(
                method,
                url,
                params=qparams,
                headers=self._get_headers(),
                timeout=timeout or self.default_timeout,
            )
            elapsed = time.time() - start
            h = {k.lower(): v for k, v in resp.headers.items()}

            # Header-aware rate limiting: honor server signals
            retry_after = None
            if "retry-after" in h:
                retry_after = float(h["retry-after"])
            elif "ratelimit-reset" in h:
                try:
                    retry_after = max(0, float(h["ratelimit-reset"]) - time.time())
                except Exception:
                    retry_after = None

            remaining = h.get("x-ratelimit-remaining") or h.get("ratelimit-remaining")
            if remaining is not None:
                try:
                    if int(remaining) < 2:
                        self.bucket.tokens = min(self.bucket.tokens, 1)
                except Exception:
                    pass

            if resp.status_code == 200:
                self._log_request(
                    cutoff_id, request_type, weight, "ok",
                    {"elapsed_ms": int(elapsed * 1000), "headers": dict(resp.headers), "remaining": remaining},
                    db_path=db_path
                )
                data = resp.json()
                return {"status": "ok", "data": data}

            elif resp.status_code == 429:
                ra = retry_after or float(h.get("retry-after", 5.0))
                self.bucket.tokens = 0
                self._log_request(cutoff_id, request_type, weight, "429", {"retry_after": ra}, db_path=db_path)
                return {"status": "unavailable", "reason": "rate_limited", "retry_after": ra}

            else:
                self._log_request(cutoff_id, request_type, weight, f"http_{resp.status_code}", {"text": resp.text[:200]}, db_path=db_path)
                return {"status": "unavailable", "reason": f"http_{resp.status_code}"}

        except httpx.TimeoutException:
            self._log_request(cutoff_id, request_type, weight, "timeout", {}, db_path=db_path)
            return {"status": "unavailable", "reason": "timeout"}
        except Exception as exc:
            self._log_request(cutoff_id, request_type, weight, "error", {"exc": str(exc)[:200]}, db_path=db_path)
            return {"status": "unavailable", "reason": "exception"}

    def close(self):
        self._client.close()
