"""Low-cost, point-in-time discovery rankings and deep-watchlist lifecycle."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import config


POOLS = ("ignition", "continuation")


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _number(record: dict, name: str, default: float = 0.0) -> float:
    value = record.get(name, default)
    return default if value is None else float(value)


def _eligible(record: dict) -> bool:
    return (
        record.get("eligible", True)
        and record.get("data_fresh", True)
        and record.get("history_warmed", True)
        and _number(record, "volume_24h_usd") >= config.SCANNER_MIN_24H_VOLUME_USD
    )


def score_ignition(record: dict) -> float | None:
    """Score a quiet, liquid base; already-moving contracts are excluded."""
    price_1h = abs(_number(record, "price_change_1h"))
    price_24h = abs(_number(record, "price_change_24h"))
    volume_z = _number(record, "volume_zscore")
    if not _eligible(record) or record.get("fresh_breakout") or record.get("post_breakout_pullback"):
        return None
    if price_1h > 0.03 or price_24h > 0.10 or volume_z > 1.5:
        return None

    quiet_price = 30 * (1 - _clamp(price_1h / 0.03))
    compression = 20 * (1 - _clamp(_number(record, "price_range_percentile", 0.5)))
    oi_pressure = _number(record, "oi_change_1h") - price_1h
    oi_score = 25 * _clamp(oi_pressure / 0.05)
    funding_score = 15 * (1 - _clamp(abs(_number(record, "funding_zscore")) / 2))
    positioning_score = 10 * _clamp(abs(_number(record, "long_short_ratio_change")) / 0.10)
    return round(quiet_price + compression + oi_score + funding_score + positioning_score, 4)


def score_continuation(record: dict) -> float | None:
    """Score liquid, participating movement without allowing thin OI to win."""
    if not _eligible(record) or record.get("exhausted_expansion"):
        return None
    volume_z = _number(record, "volume_zscore")
    oi_change = max(_number(record, "oi_change_1h"), 0.0)
    movement = _number(record, "price_change_1h")
    if volume_z <= 0 or movement < 0.002:
        return None

    participation = 35 * _clamp(volume_z / 3)
    oi_score = 30 * _clamp(oi_change / 0.08)
    movement_score = 25 * _clamp(movement / 0.05)
    positioning_score = 10 * _clamp(abs(_number(record, "long_short_ratio_change")) / 0.10)
    return round(participation + oi_score + movement_score + positioning_score, 4)


def rank_pools(records: list[dict], top_n: int = config.DISCOVERY_TOP_N) -> dict[str, list[dict]]:
    """Return independent rankings. Each input record remains independent of peers."""
    ranked = {pool: [] for pool in POOLS}
    for record in records:
        item = dict(record)
        for pool, scorer in (("ignition", score_ignition), ("continuation", score_continuation)):
            score = scorer(item)
            if score is not None:
                ranked[pool].append({**item, "score": score})
    for pool in POOLS:
        ranked[pool] = sorted(ranked[pool], key=lambda item: item["score"], reverse=True)[:top_n]
        for rank, item in enumerate(ranked[pool], 1):
            item["rank"] = rank
    return ranked


def _current_watchlist(conn) -> dict[tuple[str, str], tuple]:
    rows = conn.execute("""
        SELECT pool, symbol, state, entered_at
        FROM discovery_watchlist_history
        QUALIFY ROW_NUMBER() OVER (PARTITION BY pool, symbol ORDER BY observed_at DESC, event_id DESC) = 1
    """).fetchall()
    return {(pool, symbol): (state, entered_at) for pool, symbol, state, entered_at in rows}


def _persist_snapshots(conn, observed_at: datetime, records: list[dict], rankings: dict[str, list[dict]]):
    ranks = {(pool, item["symbol"]): item for pool, items in rankings.items() for item in items}
    rows = []
    for record in records:
        ignition = ranks.get(("ignition", record["symbol"]))
        continuation = ranks.get(("continuation", record["symbol"]))
        rows.append((
            observed_at, record["symbol"], record["asset"], record.get("liquidity_tier"), _eligible(record),
            record.get("data_fresh", True), record.get("history_warmed", True), _number(record, "volume_24h_usd"),
            _number(record, "open_interest_usd"), _number(record, "volume_zscore"), _number(record, "oi_change_1h"),
            _number(record, "price_change_1h"), _number(record, "price_change_24h"), _number(record, "price_range_percentile", 0.5),
            _number(record, "funding_rate"), _number(record, "funding_zscore"), _number(record, "long_short_ratio_change"),
            bool(record.get("fresh_breakout")), bool(record.get("post_breakout_pullback")), bool(record.get("exhausted_expansion")),
            score_ignition(record), score_continuation(record), ignition["rank"] if ignition else None,
            continuation["rank"] if continuation else None,
        ))
    conn.executemany("""
        INSERT INTO broad_discovery_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)


def enqueue_deep_backfill_jobs(conn, observed_at: datetime, symbols: list[str]) -> None:
    """Queue selected symbols for durable deep-history bootstrap work."""
    rows = [(symbol, "pending", 0, observed_at, None, observed_at, observed_at, None, None)
            for symbol in dict.fromkeys(symbols)]
    if not rows:
        return
    conn.executemany("""
        INSERT INTO deep_backfill_jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (symbol) DO UPDATE SET
            status = 'pending', attempts = 0, next_retry_at = excluded.next_retry_at,
            last_error = NULL, updated_at = excluded.updated_at, started_at = NULL,
            completed_at = NULL
    """, rows)


def process_snapshot(conn, observed_at: datetime, records: list[dict], top_n: int = config.DISCOVERY_TOP_N) -> dict[str, list[dict]]:
    """Persist a broad snapshot and append watchlist transitions.

    A newly ranked asset emits ``deep_backfill_required`` exactly once per pool
    entry and queues durable deep-history work. It remains warming for 24 hours
    even when it drops from the ranking.
    """
    rankings = rank_pools(records, top_n)
    _persist_snapshots(conn, observed_at, records, rankings)
    record_by_symbol = {record["symbol"]: record for record in records}
    selected = {(pool, item["symbol"]): item for pool, items in rankings.items() for item in items}
    current = _current_watchlist(conn)
    minimum_residency = timedelta(hours=config.DISCOVERY_MIN_RESIDENCY_HOURS)
    events = []
    for key, (prior_state, entered_at) in current.items():
        if prior_state == "expired" or key in selected:
            continue
        record = record_by_symbol.get(key[1])
        stale_or_ineligible = record is None or not _eligible(record)
        elapsed = observed_at - entered_at
        if stale_or_ineligible:
            state, reason = "expired", "stale_or_ineligible"
        elif elapsed >= minimum_residency:
            state, reason = "expired", "no_longer_ranked"
        else:
            state, reason = "warming", None
        events.append((key, record, state, None, None, entered_at, False, reason))
    for key, item in selected.items():
        prior = current.get(key)
        if prior is None or prior[0] == "expired":
            state, entered_at, handoff = "entered", observed_at, True
        else:
            entered_at = prior[1]
            state = "active" if observed_at - entered_at >= minimum_residency else "warming"
            handoff = False
        events.append((key, item, state, item["rank"], item["score"], entered_at, handoff, None))
    newly_selected_symbols = []
    for (pool, symbol), record, state, rank, score, entered_at, handoff, reason in events:
        asset = record["asset"] if record else symbol.removesuffix("USDT")
        conn.execute("""
            INSERT INTO discovery_watchlist_history VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (str(uuid4()), observed_at, pool, symbol, asset, state, rank, score, entered_at, handoff, reason))
        if handoff:
            newly_selected_symbols.append(symbol)
    enqueue_deep_backfill_jobs(conn, observed_at, newly_selected_symbols)
    return rankings
