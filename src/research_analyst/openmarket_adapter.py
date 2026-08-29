"""OpenMarket Free adapter (compat shim).

Delegates to the professional implementation in api_clients.openmarket.
See specs/external-api-rate-limiting.md for full contract.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import config
from api_clients.openmarket import OpenMarketClient


def fetch_htf_profile(assets: List[str], cutoff_id: str, deadline_ms: int = None, db_path: str | None = None) -> Dict[str, Any]:
    client = OpenMarketClient()
    return client.fetch_htf_profile(assets, cutoff_id, db_path=db_path)


def fetch_15m_flow(assets: List[str], cutoff_id: str, deadline_ms: int = None, db_path: str | None = None) -> Dict[str, Any]:
    client = OpenMarketClient()
    return client.fetch_15m_flow(assets, cutoff_id, db_path=db_path)
