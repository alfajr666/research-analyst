"""Dedicated outcome evaluator.

Per spec:
- Market read-only from source_observations (post drop of legacy futures_data).
- Alpha DB write-only for outcomes.
- Called from orchestrator and publisher.
- Never blocks.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import config
from alpha_research import record_outcome


def parse_timestamp(value: str) -> datetime:
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return ts.astimezone(timezone.utc)


def _compute_returns_and_excursions(
    direction: str,
    entry: float,
    entry_at: datetime,
    outcome_bars: list[tuple],
    targets: list[float],
    valid_until: datetime,
) -> dict[str, Any]:
    target = min(targets) if direction == "long" else max(targets)
    # simplified: use existing barrier logic in caller

    def return_at(minutes: int) -> float | None:
        target_at = entry_at + timedelta(minutes=minutes)
        bar = next((item for item in outcome_bars if item[0] >= target_at), None)
        if bar is None:
            return None
        raw = float(bar[3]) / entry - 1
        return raw if direction == "long" else -raw

    returns = (return_at(15), return_at(60), return_at(240))
    highs = [float(bar[1]) / entry - 1 for bar in outcome_bars]
    lows = [float(bar[2]) / entry - 1 for bar in outcome_bars]
    favorable = max(highs) if direction == "long" else -min(lows)
    adverse = min(lows) if direction == "long" else -max(highs)

    return {
        "return_15m": returns[0],
        "return_1h": returns[1],
        "return_4h": returns[2],
        "max_favorable_excursion": favorable,
        "max_adverse_excursion": adverse,
    }


def evaluate_expired_outcomes(
    market_db_path: str, alpha_db_path: str, now: datetime | None = None,
    market_conn: Any = None
) -> int:
    """Evaluate expired alpha events using market bars only. Write outcomes to alpha.

    Returns number of outcomes recorded.
    If market_conn provided (same file), reuse it (avoid read_only mix).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    alpha_conn = config.get_db_connection(db_path=alpha_db_path)
    try:
        rows = alpha_conn.execute(
            """
            SELECT alpha_id, event_json FROM alpha_events
            WHERE status = 'expired'
              AND alpha_id NOT IN (SELECT candidate_id FROM alpha_outcomes)
            """
        ).fetchall()
    finally:
        alpha_conn.close()

    if not rows:
        return 0

    close_market = False
    if market_conn is None:
        # only open read_only if different path (tests often share file; avoid config mix)
        if market_db_path == alpha_db_path:
            market_conn = config.get_db_connection(read_only=False, db_path=market_db_path)
        else:
            market_conn = config.get_db_connection(read_only=True, db_path=market_db_path)
        close_market = True
    recorded = 0
    try:
        for candidate_id, serialized_event in rows:
            event = json.loads(serialized_event)
            snapshot = event.get("feature_snapshot", {})
            symbol = snapshot.get("source_symbol") or event.get("asset")
            if not symbol:
                continue
            observed_at = parse_timestamp(event["observed_at"])
            valid_until = parse_timestamp(event["valid_until"])
            bars = market_conn.execute(
                """
                SELECT 
                    source_end as timestamp,
                    json_extract(payload_json, '$.high')::DOUBLE as high,
                    json_extract(payload_json, '$.low')::DOUBLE as low,
                    json_extract(payload_json, '$.close')::DOUBLE as close
                FROM source_observations
                WHERE native_symbol = ? 
                  AND source_end >= ? AND source_end <= ? 
                  AND json_extract(payload_json, '$.close')::DOUBLE > 0
                ORDER BY source_end
                """,
                (symbol, observed_at, valid_until),
            ).fetchall()
            if not bars:
                continue

            entry = float(event["entry_condition"].get("price", bars[0][3]))
            direction = event["direction"]
            trigger = event["entry_condition"]["type"]
            targets = event.get("targets", [])

            def triggered(bar) -> bool:
                _, high, low, _ = bar
                if trigger == "breakout_above":
                    return high >= entry
                if trigger == "breakout_below":
                    return low <= entry
                return low <= entry if direction == "long" else high >= entry

            entry_index = next((i for i, bar in enumerate(bars) if triggered(bar)), None)
            if entry_index is None:
                outcome = "not_triggered"
                entry_at = None
                outcome_bars = []
                excursions = {"return_15m": None, "return_1h": None, "return_4h": None,
                              "max_favorable_excursion": None, "max_adverse_excursion": None}
            else:
                entry_at = bars[entry_index][0]
                later_bars = bars[entry_index:]
                invalidation = float(event["invalidation_price"])

                def barrier_status(bar) -> tuple[bool, bool]:
                    _, high, low, _ = bar
                    if direction == "long":
                        return high >= (min(targets) if targets else 0), low <= invalidation
                    return low <= (max(targets) if targets else 0), high >= invalidation

                terminal_index = None
                for idx, bar in enumerate(later_bars):
                    th, inv = barrier_status(bar)
                    if idx == 0 and (th or inv):
                        outcome = "ambiguous_same_bar"
                        terminal_index = idx
                        break
                    if th and inv:
                        outcome = "ambiguous_same_bar"
                        terminal_index = idx
                        break
                    if th:
                        outcome = "target"
                        terminal_index = idx
                        break
                    if inv:
                        outcome = "invalidated"
                        terminal_index = idx
                        break
                else:
                    outcome = "expired"
                outcome_bars = later_bars if terminal_index is None else later_bars[:terminal_index + 1]
                excursions = _compute_returns_and_excursions(
                    direction, entry, entry_at, outcome_bars, targets, valid_until
                )

            outcome_rec = {
                "evaluated_at": now,
                "entry_at": entry_at,
                "entry_price": entry if entry_index is not None else None,
                "outcome": outcome,
                "expiry_at": valid_until,
                "return_15m": excursions.get("return_15m"),
                "return_1h": excursions.get("return_1h"),
                "return_4h": excursions.get("return_4h"),
                "max_favorable_excursion": excursions.get("max_favorable_excursion"),
                "max_adverse_excursion": excursions.get("max_adverse_excursion"),
                "estimated_cost": None,
                "net_return": None,
                "details": {"bars_observed": len(bars), "same_bar_policy": "ambiguous"},
            }
            # write using shared (ensures schema)
            alpha_write = config.get_db_connection(db_path=alpha_db_path)
            try:
                record_outcome(alpha_write, candidate_id, outcome_rec)
            finally:
                alpha_write.close()
            recorded += 1
    finally:
        if close_market:
            market_conn.close()
    return recorded
