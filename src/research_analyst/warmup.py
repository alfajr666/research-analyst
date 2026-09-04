"""Durable deep-history readiness for rotated market assets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

import config
from market_coverage import CoverageResult, assess_db_coverage


@dataclass(frozen=True)
class WarmupResult:
    asset: str
    ready: bool
    coverage: CoverageResult
    reason: str

    def as_dict(self) -> dict[str, Any]:
        result = self.coverage.as_dict()
        result.update({"ready": self.ready, "reason": self.reason})
        return result


def required_5m_bars() -> int:
    """Return enough base bars for ATR and confirmed 4h structure warmup."""
    atr_bars = int(getattr(config, "DEEP_WARMUP_4H_ATR_BARS", 14))
    structure_bars = int(getattr(config, "DEEP_WARMUP_SWING_LOOKBACK", 20)) + 2
    return max(atr_bars, structure_bars, 3) * 4 * 60 // 5


def assess_asset_warmup(conn: Any, asset: str, cutoff: datetime) -> WarmupResult:
    """Assess a rotated asset using only closed, point-in-time 5m observations."""
    coverage = assess_db_coverage(
        conn,
        asset=asset,
        interval="5m",
        cutoff=cutoff,
        expected_bars=required_5m_bars(),
        max_age_seconds=float(getattr(config, "DATA_FRESHNESS_MAX_SECONDS", 600)),
    )
    ready = coverage.status == "covered"
    reason = "ready" if ready else f"5m deep warmup {coverage.status}"
    return WarmupResult(str(asset).upper(), ready, coverage, reason)


def ready_assets(conn: Any, assets: Iterable[str], cutoff: datetime) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Return only assets whose deep history is complete at the cutoff."""
    results = [assess_asset_warmup(conn, asset, cutoff) for asset in sorted({str(a).upper() for a in assets})]
    return [result.asset for result in results if result.ready], {
        result.asset: result.as_dict() for result in results
    }


def ensure_backfill_jobs(conn: Any, assets: Iterable[str], now: datetime | None = None) -> int:
    """Create pending jobs for newly selected assets, preserving completed jobs."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    created = 0
    for asset in sorted({str(a).upper() for a in assets if str(a).strip()}):
        result = conn.execute(
            """INSERT INTO deep_backfill_jobs
                   (symbol, status, attempts, next_retry_at, created_at, updated_at)
                VALUES (?, 'pending', 0, ?, ?, ?)
                ON CONFLICT(symbol) DO NOTHING""",
            (asset, now, now, now),
        )
        created += int(getattr(result, "rowcount", 0) or 0)
    conn.commit()
    return created


def refresh_backfill_jobs(conn: Any, assets: Iterable[str], cutoff: datetime,
                          now: datetime | None = None) -> dict[str, str]:
    """Mark jobs complete only after coverage proves the required history exists."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    states: dict[str, str] = {}
    for asset in sorted({str(a).upper() for a in assets if str(a).strip()}):
        result = assess_asset_warmup(conn, asset, cutoff)
        status = "completed" if result.ready else "pending"
        states[result.asset] = status
        conn.execute(
            """UPDATE deep_backfill_jobs
                  SET status=?, updated_at=?, completed_at=?
                WHERE symbol=?""",
            (status, now, now if result.ready else None, result.asset),
        )
    conn.commit()
    return states
