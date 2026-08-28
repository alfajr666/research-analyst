"""Binance USD-M futures public client for venue-agg failover.

Thin wrapper over RateLimitedClient. Logs all calls to source_request_log as binance_usdm.
Public endpoints only (no key). Follows external-api-rate-limiting + failover spec.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import config
from api_clients.base import RateLimitedClient


class BinanceFuturesClient(RateLimitedClient):
    def __init__(self):
        super().__init__(
            base_url=config.BINANCE_FUTURES_BASE_URL,
            api_key="public",  # truthy to bypass no-key path; not sent
            source_name="binance_usdm",
            rate_per_sec=0.2,
            default_timeout=12.0,
        )
        self.auth_in_query = False  # never append api_key to public URLs

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
        interval: str = "15m",
        cutoff_id: str = "failover",
    ) -> List[List[Any]]:
        symbol = self._asset_to_symbol(asset)
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 500,
        }
        result = self.request(
            "GET",
            "/fapi/v1/klines",
            params=params,
            cutoff_id=cutoff_id,
            request_type="klines_15m",
            weight=1,
        )
        if result.get("status") == "ok":
            data = result.get("data", [])
            return data if isinstance(data, list) else []
        return []

    def fetch_oi_hist(
        self,
        asset: str,
        start_ms: int,
        end_ms: int,
        period: str = "15m",
        cutoff_id: str = "failover",
    ) -> List[Dict[str, Any]]:
        symbol = self._asset_to_symbol(asset)
        params = {
            "symbol": symbol,
            "period": period,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 500,
        }
        result = self.request(
            "GET",
            "/futures/data/openInterestHist",
            params=params,
            cutoff_id=cutoff_id,
            request_type="oi_hist",
            weight=2,
        )
        if result.get("status") == "ok":
            data = result.get("data", [])
            return data if isinstance(data, list) else []
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
            "symbol": symbol,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 100,
        }
        result = self.request(
            "GET",
            "/fapi/v1/fundingRate",
            params=params,
            cutoff_id=cutoff_id,
            request_type="funding",
            weight=1,
        )
        if result.get("status") == "ok":
            data = result.get("data", [])
            return data if isinstance(data, list) else []
        return []
