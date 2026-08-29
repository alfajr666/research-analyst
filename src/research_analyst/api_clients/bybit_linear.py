"""Bybit linear (USDT perp) public client for venue-agg failover.

Thin wrapper over RateLimitedClient. Logs as bybit_linear.
Public endpoints. Per spec: best-effort for 15m klines/OI/funding.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import config
from api_clients.base import RateLimitedClient


class BybitLinearClient(RateLimitedClient):
    def __init__(self):
        super().__init__(
            base_url=config.BYBIT_LINEAR_BASE_URL,
            api_key="public",
            source_name="bybit_linear",
            rate_per_sec=0.2,
            default_timeout=12.0,
        )
        self.auth_in_query = False

    def _asset_to_symbol(self, asset: str) -> str:
        a = asset.upper()
        if a in ("BTC", "ETH", "SOL"):
            return f"{a}USDT"
        return f"{a}USDT"

    def fetch_klines(
        self,
        asset: str,
        start_ms: int,
        end_ms: int,
        interval: str = "15",
        cutoff_id: str = "failover",
    ) -> List[Dict[str, Any]]:
        symbol = self._asset_to_symbol(asset)
        params = {
            "category": "linear",
            "symbol": symbol,
            "interval": interval,
            "start": start_ms,
            "end": end_ms,
            "limit": 200,
        }
        result = self.request(
            "GET",
            "/v5/market/kline",
            params=params,
            cutoff_id=cutoff_id,
            request_type="kline_15m",
            weight=1,
        )
        if result.get("status") == "ok":
            data = result.get("data", {})
            # Bybit v5: {"retCode":0, "result": {"list": [[ts,open,high,...], ...] } }
            if isinstance(data, dict):
                lst = data.get("result", {}).get("list", []) if "result" in data else data.get("list", [])
                return lst if isinstance(lst, list) else []
            return []
        return []

    def fetch_oi(
        self,
        asset: str,
        start_ms: int,
        end_ms: int,
        interval: str = "15m",
        cutoff_id: str = "failover",
    ) -> List[Dict[str, Any]]:
        symbol = self._asset_to_symbol(asset)
        params = {
            "category": "linear",
            "symbol": symbol,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 200,
        }
        result = self.request(
            "GET",
            "/v5/market/open-interest",
            params=params,
            cutoff_id=cutoff_id,
            request_type="oi",
            weight=2,
        )
        if result.get("status") == "ok":
            data = result.get("data", {})
            if isinstance(data, dict):
                lst = data.get("result", {}).get("list", []) if isinstance(data.get("result"), dict) else []
                return lst if isinstance(lst, list) else []
            return []
        return []

    def fetch_funding(
        self,
        asset: str,
        start_ms: int,
        end_ms: int,
        cutoff_id: str = "failover",
    ) -> List[Dict[str, Any]]:
        symbol = self._asset_to_symbol(asset)
        params = {
            "category": "linear",
            "symbol": symbol,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 200,
        }
        result = self.request(
            "GET",
            "/v5/market/funding/history",
            params=params,
            cutoff_id=cutoff_id,
            request_type="funding",
            weight=1,
        )
        if result.get("status") == "ok":
            data = result.get("data", {})
            if isinstance(data, dict):
                lst = data.get("result", {}).get("list", []) if isinstance(data.get("result"), dict) else []
                return lst if isinstance(lst, list) else []
            return []
        return []
