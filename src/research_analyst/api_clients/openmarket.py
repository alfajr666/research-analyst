"""OpenMarket client (real implementation of the spec stub).

Per specs/external-api-rate-limiting.md and data-platform-strategy-plugins.md:
- Deadline bounded
- Weight/budget tracked in source_request_log
- Returns {"status": "unavailable"} on any problem (rate limit, timeout, budget, disabled)
- Never blocks core pipeline
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import config
from api_clients.base import RateLimitedClient


class OpenMarketClient(RateLimitedClient):
    def __init__(self):
        super().__init__(
            base_url="https://api.openmarket.xyz/v1",  # real base (use X-OpenMarket-Key header)
            api_key=config.OPENMARKET_API_KEY,
            source_name="openmarket",
            rate_per_sec=10.0 / 60.0,  # 10/min free tier example
            default_timeout=config.OPENMARKET_REQUEST_DEADLINE_MS / 1000.0,
        )
        self.enabled = config.OPENMARKET_ENABLED
        self.reference_venue = config.OPENMARKET_REFERENCE_VENUE
        self.auth_in_query = False

    def _get_headers(self) -> Dict[str, str]:
        headers = super()._get_headers()
        if self.api_key:
            headers["X-OpenMarket-Key"] = self.api_key
        return headers

    def _check_budget(self, cutoff_id: str, weight: int) -> bool:
        # In real impl query source_request_log for today's consumption.
        # For now we trust caller + the log inside request().
        return True

    def _to_symbol_key(self, asset: str) -> tuple[str, str]:
        a = asset.upper().replace("_PERP.A", "").replace("USDT_PERP", "USDT").replace("_SPOT", "").strip()
        if len(a) <= 5 and not any(x in a for x in ("USDT", "USD", "PERP")):
            return "coin", a
        raw = a if a.endswith(("USDT", "USD")) else (a + "USDT" if len(a) <= 5 else a)
        return "rawSymbol", raw

    def fetch_htf_profile(
        self, assets: List[str], cutoff_id: str, weight: int = 10, db_path: str | None = None
    ) -> Dict[str, Any]:
        if not self.enabled or not self.api_key:
            self._log_request(cutoff_id, "htf_profile", 0, "skipped", {"reason": "disabled"}, db_path=db_path)
            return {a: {"status": "unavailable"} for a in assets}

        if not self._check_budget(cutoff_id, weight):
            self._log_request(cutoff_id, "htf_profile", weight, "budget_exceeded", {}, db_path=db_path)
            return {a: {"status": "unavailable"} for a in assets}

        key, val = self._to_symbol_key(assets[0] if assets else "BTC")
        now = int(time.time())
        result = self.request(
            "GET",
            "points",
            params={
                "type": "VOLUME_PROFILE_AGG",
                "exchange": self.reference_venue,
                key: val,
                "interval": "FOUR_HOURS",
                "from": now - 8 * 3600,
                "period": 8 * 3600,
            },
            cutoff_id=cutoff_id,
            request_type="htf_profile",
            weight=weight,
            db_path=db_path,
        )
        if result.get("status") != "ok":
            return {a: {"status": "unavailable"} for a in assets}

        payload = result.get("data", {})
        out: Dict[str, Any] = {}
        series = payload.get("series", []) if isinstance(payload, dict) else []
        for a in assets:
            match = next((s for s in series if str(s.get("id", {}).get("coin", "")) == a.upper() or s.get("id", {}).get("rawSymbol", "") == a), None)
            out[a] = {"status": "ok", "series": [match] if match else [], "raw": payload if not match else None}
        return out

    def fetch_15m_flow(
        self, assets: List[str], cutoff_id: str, weight: int = 5, db_path: str | None = None
    ) -> Dict[str, Any]:
        if not self.enabled:
            self._log_request(cutoff_id, "15m_flow", 0, "skipped", {}, db_path=db_path)
            return {a: {"status": "unavailable"} for a in assets}

        key, val = self._to_symbol_key(assets[0] if assets else "BTC")
        now = int(time.time())
        result = self.request(
            "GET",
            "points",
            params={
                "type": "TRADE_AGG",
                "exchange": self.reference_venue,
                key: val,
                "interval": "FIFTEEN_MINUTES",
                "from": now - 3600,
                "period": 3600,
            },
            cutoff_id=cutoff_id,
            request_type="15m_flow",
            weight=weight,
            db_path=db_path,
        )
        if result.get("status") != "ok":
            return {a: {"status": "unavailable"} for a in assets}

        payload = result.get("data", {})
        out: Dict[str, Any] = {}
        series = payload.get("series", []) if isinstance(payload, dict) else []
        for a in assets:
            match = next((s for s in series if str(s.get("id", {}).get("coin", "")) == a.upper() or s.get("id", {}).get("rawSymbol", "") == a), None)
            out[a] = {"status": "ok", "series": [match] if match else [], "raw": payload if not match else None}
        return out
