"""Curated read-only local evidence adapters."""

from __future__ import annotations

import json
from datetime import datetime


def _evidence(source_type: str, source_ref: str, as_of: datetime, value: object) -> dict:
    return {"evidence_id": f"local:{source_type}:{source_ref}", "source_type": source_type,
            "source_ref": source_ref, "as_of": as_of.isoformat(), "value": value}


class ResearchTools:
    def __init__(self, connection, as_of: datetime):
        self.connection, self.as_of = connection, as_of

    def get_event(self, alpha_id: str) -> dict:
        row = self.connection.execute("SELECT event_json FROM alpha_events WHERE alpha_id = ? AND observed_at <= ?", (alpha_id, self.as_of)).fetchone()
        if row is None:
            raise ValueError("event is unavailable at the requested cutoff")
        return _evidence("alpha_events", alpha_id, self.as_of, json.loads(row[0]))

    def get_completed_bars(self, symbol: str, window: int = 96) -> dict:
        try:
            rows = self.connection.execute("SELECT source_end as timestamp, json_extract(payload_json, '$.open')::DOUBLE as open, json_extract(payload_json, '$.high')::DOUBLE as high, json_extract(payload_json, '$.low')::DOUBLE as low, json_extract(payload_json, '$.close')::DOUBLE as close, json_extract(payload_json, '$.volume')::DOUBLE as volume FROM source_observations WHERE native_symbol = ? AND source_end < ? ORDER BY source_end DESC LIMIT ?", (symbol, self.as_of, min(window, 96))).fetchall()
        except Exception:
            rows = []
        return _evidence("source_observations", symbol, self.as_of, [{"timestamp": row[0].isoformat(), "open": row[1], "high": row[2], "low": row[3], "close": row[4], "volume": row[5]} for row in reversed(rows)])

    def get_discovery_context(self, asset: str) -> dict:
        try:
            row = self.connection.execute("SELECT observed_at, liquidity_tier, data_fresh, history_warmed FROM broad_discovery_snapshots WHERE asset = ? AND observed_at <= ? ORDER BY observed_at DESC LIMIT 1", (asset, self.as_of)).fetchone()
        except Exception:
            row = None
        value = None if row is None else {"observed_at": row[0].isoformat(), "liquidity_tier": row[1], "data_fresh": row[2], "history_warmed": row[3]}
        return _evidence("broad_discovery_snapshots", asset, self.as_of, value)

    def get_regime_context(self, asset: str) -> dict:
        # Daily rows lack an intra-day timestamp, so only prior completed dates
        # are eligible; same-day data could have been created after the cutoff.
        try:
            row = self.connection.execute("SELECT date, signal, regime, regime_conf FROM regime_signals WHERE underlying = ? AND date < CAST(? AS DATE) ORDER BY date DESC LIMIT 1", (asset, self.as_of)).fetchone()
        except Exception:
            row = None
        value = None if row is None else {"date": row[0].isoformat(), "signal": row[1], "regime": row[2], "regime_conf": row[3]}
        return _evidence("regime_signals", asset, self.as_of, value)

    def get_prior_outcomes(self, strategy: str, tier: str) -> dict:
        rows = self.connection.execute("SELECT c.candidate_id, c.observed_at, o.outcome, o.net_return FROM alpha_candidates c JOIN alpha_outcomes o USING (candidate_id) WHERE c.strategy_id = ? AND c.liquidity_tier = ? AND c.observed_at <= ? ORDER BY c.observed_at DESC LIMIT 50", (strategy, tier, self.as_of)).fetchall()
        return _evidence("alpha_outcomes", f"{strategy}:{tier}", self.as_of, {"count": len(rows), "label": "descriptive history, not probability calibration", "rows": [{"candidate_id": row[0], "observed_at": row[1].isoformat(), "outcome": row[2], "net_return": row[3]} for row in rows]})

    def get_data_quality(self, symbol: str) -> dict:
        try:
            latest, count = self.connection.execute("SELECT MAX(source_end), COUNT(*) FROM source_observations WHERE native_symbol = ? AND source_end < ?", (symbol, self.as_of)).fetchone()
        except Exception:
            latest, count = None, 0
        return _evidence("source_observations", f"quality:{symbol}", self.as_of, {"bars_available": count or 0, "latest_bar_at": latest.isoformat() if latest else None, "freshness_seconds": (self.as_of - latest).total_seconds() if latest else None})
