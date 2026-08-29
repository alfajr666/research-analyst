"""CoinAnalyze client following specs/external-api-rate-limiting.md."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import config
from api_clients.base import RateLimitedClient


class CoinAnalyzeClient(RateLimitedClient):
    def __init__(self):
        super().__init__(
            base_url=config.COINANALYZE_BASE_URL,
            api_key=config.COINANALYZE_API_KEY,
            source_name="coinalyze",
            rate_per_sec=float(getattr(config, "COINANALYZE_RPS", 0.08)),
            max_concurrent=int(getattr(config, "COINANALYZE_MAX_CONCURRENT", 5)),
        )

    def fetch(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        cutoff_id: str = "no-cutoff",
        weight: int = 1,
        db_path: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Thin wrapper that returns list or [] on any failure."""
        result = self.request(
            "GET",
            endpoint,
            params=params,
            cutoff_id=cutoff_id,
            request_type=endpoint,
            weight=weight,
            db_path=db_path,
        )
        if result.get("status") == "ok":
            data = result.get("data", [])
            return data if isinstance(data, list) else [data]
        return []

    def fetch_batched(
        self,
        endpoint: str,
        symbols: List[str],
        other_params: Optional[Dict] = None,
        batch_size: int = 30,
        cutoff_id: str = "no-cutoff",
        weight: int = 1,
        db_path: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Batched fetch to stay under limits."""
        if not symbols:
            return []
        combined: List[Dict] = []
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i : i + batch_size]
            params = dict(other_params or {})
            params["symbols"] = ",".join(batch)
            res = self.fetch(endpoint, params, cutoff_id, weight, db_path=db_path)
            combined.extend(res)
        return combined
