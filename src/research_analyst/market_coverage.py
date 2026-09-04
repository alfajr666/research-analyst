"""Point-in-time, per-asset market coverage checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from typing import Any, Iterable, Mapping
import json


INTERVAL_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


def _utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _normalize_source_end(value: datetime | str) -> datetime:
    """Map exchange boundary-minus-one-millisecond stamps to their boundary."""
    value = _utc(value)
    if value.microsecond >= 999_000:
        value += timedelta(milliseconds=1)
    return value.replace(microsecond=0)


def _floor_boundary(value: datetime, seconds: int) -> datetime:
    epoch = int(_utc(value).timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, tz=timezone.utc)


@dataclass(frozen=True)
class CoverageResult:
    asset: str
    interval: str
    cutoff: datetime
    latest_end: datetime | None
    freshness_seconds: float | None
    expected_bars: int
    observed_bars: int
    missing_ends: tuple[datetime, ...]
    duplicate_ends: tuple[datetime, ...]
    max_gap_seconds: int | None
    sources: tuple[str, ...]
    purity: str
    status: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset": self.asset,
            "interval": self.interval,
            "cutoff": self.cutoff.isoformat().replace("+00:00", "Z"),
            "latest_end": self.latest_end.isoformat().replace("+00:00", "Z") if self.latest_end else None,
            "freshness_seconds": self.freshness_seconds,
            "expected_bars": self.expected_bars,
            "observed_bars": self.observed_bars,
            "missing_ends": [value.isoformat().replace("+00:00", "Z") for value in self.missing_ends],
            "duplicate_ends": [value.isoformat().replace("+00:00", "Z") for value in self.duplicate_ends],
            "max_gap_seconds": self.max_gap_seconds,
            "sources": list(self.sources),
            "purity": self.purity,
            "status": self.status,
        }


def _purity(rows: Iterable[Mapping[str, Any]]) -> str:
    values = {str(row.get("purity") or "").strip() for row in rows if row.get("purity")}
    if not values:
        return "unknown"
    if len(values) > 1:
        return "mixed"
    return values.pop()


def assess_coverage(
    rows: Iterable[Mapping[str, Any]],
    *,
    asset: str,
    interval: str,
    cutoff: datetime,
    expected_bars: int,
    max_age_seconds: float | None = None,
) -> CoverageResult:
    """Assess one asset's expected closed-bar window at a cutoff.

    ``source_end`` is the exclusive end of a closed bar. The expected window
    ends at the most recent boundary for ``interval`` at or before ``cutoff``.
    """
    if interval not in INTERVAL_SECONDS:
        raise ValueError(f"unsupported coverage interval: {interval}")
    if expected_bars <= 0:
        raise ValueError("expected_bars must be positive")
    cutoff = _utc(cutoff)
    seconds = INTERVAL_SECONDS[interval]
    boundary = _floor_boundary(cutoff, seconds)
    expected = tuple(
        datetime.fromtimestamp(boundary.timestamp() - seconds * offset, tz=timezone.utc)
        for offset in range(expected_bars - 1, -1, -1)
    )

    timestamps: list[datetime] = []
    raw_timestamps: dict[datetime, list[datetime]] = {}
    sources: set[str] = set()
    purity_rows: list[Mapping[str, Any]] = []
    invalid = False
    for row in rows:
        try:
            raw_timestamp = _utc(row["source_end"])
            timestamp = _normalize_source_end(raw_timestamp)
        except (KeyError, TypeError, ValueError, OverflowError):
            invalid = True
            continue
        if timestamp > cutoff:
            continue
        if row.get("_valid") is False and timestamp in expected:
            invalid = True
        timestamps.append(timestamp)
        raw_timestamps.setdefault(timestamp, []).append(raw_timestamp)
        source = str(row.get("source") or "").strip()
        if source:
            sources.add(source)
        purity_rows.append(row)

    timestamps.sort()
    unique = set(timestamps)
    duplicate_ends = tuple(sorted(
        timestamp
        for timestamp, values in raw_timestamps.items()
        if len(values) > 1
        and not (
            len(values) == 2
            and sum(value == timestamp for value in values) == 1
            and sum(_normalize_source_end(value) == timestamp for value in values) == 2
        )
    ))
    observed = tuple(timestamp for timestamp in timestamps if timestamp in set(expected))
    observed_set = set(observed)
    missing = tuple(timestamp for timestamp in expected if timestamp not in observed_set)
    latest = max(unique) if unique else None
    freshness = (cutoff - latest).total_seconds() if latest else None
    gaps = [int((right - left).total_seconds()) for left, right in zip(sorted(unique), sorted(unique)[1:])]
    max_gap = max(gaps) if gaps else None

    status = "covered"
    if latest is None:
        status = "missing"
    elif max_age_seconds is not None and (freshness is None or freshness < 0 or freshness > max_age_seconds):
        status = "stale"
    elif invalid or duplicate_ends or missing or len(observed_set) != expected_bars:
        status = "incomplete"
    elif len(sources) > 1 or _purity(purity_rows) == "mixed":
        status = "mixed_source"

    return CoverageResult(
        asset=str(asset).upper(),
        interval=interval,
        cutoff=cutoff,
        latest_end=latest,
        freshness_seconds=freshness,
        expected_bars=expected_bars,
        observed_bars=len(observed_set),
        missing_ends=missing,
        duplicate_ends=duplicate_ends,
        max_gap_seconds=max_gap,
        sources=tuple(sorted(sources)),
        purity=_purity(purity_rows),
        status=status,
    )


def validate_ohlcv_payload(payload: Mapping[str, Any]) -> bool:
    """Return whether a persisted OHLC payload is finite and geometrically valid."""
    try:
        values = {name: float(payload[name]) for name in ("open", "high", "low", "close")}
    except (KeyError, TypeError, ValueError):
        return False
    if not all(math.isfinite(value) and value > 0 for value in values.values()):
        return False
    return (
        values["high"] >= max(values["open"], values["close"])
        and values["low"] <= min(values["open"], values["close"])
    )


def assess_db_coverage(
    conn: Any,
    *,
    asset: str,
    interval: str,
    cutoff: datetime,
    expected_bars: int,
    max_age_seconds: float | None = None,
) -> CoverageResult:
    """Load and assess source observations for one asset without writing."""
    try:
        rows = conn.execute(
            """SELECT source_end, source, payload_json
                 FROM source_observations
                WHERE asset = ? AND interval = ? AND source_end <= ?
                ORDER BY source_end ASC""",
            (str(asset).upper(), interval, _utc(cutoff) + timedelta(milliseconds=1)),
        ).fetchall()
    except Exception:
        return assess_coverage(
            [], asset=asset, interval=interval, cutoff=cutoff,
            expected_bars=expected_bars, max_age_seconds=max_age_seconds,
        )
    mapped = []
    for source_end, source, payload_json in rows:
        payload: Mapping[str, Any] = {}
        try:
            payload = json.loads(payload_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        source_name = str(source or "")
        purity = "pure_ws" if source_name.endswith("_ws") else "unknown"
        mapped.append({
            "source_end": source_end,
            "source": source_name,
            "purity": purity,
            "_valid": validate_ohlcv_payload(payload),
        })
    return assess_coverage(
        mapped,
        asset=asset,
        interval=interval,
        cutoff=cutoff,
        expected_bars=expected_bars,
        max_age_seconds=max_age_seconds,
    )
