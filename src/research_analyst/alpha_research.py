"""Persistence helpers for point-in-time alpha research records."""

import json
from datetime import datetime

import config


def classify_liquidity_tier(volume_24h_usd: float) -> str:
    """Classify a contract from its point-in-time 24-hour Binance notional."""
    if volume_24h_usd >= config.SCANNER_CORE_24H_VOLUME_USD:
        return "core"
    if volume_24h_usd >= config.SCANNER_MIN_24H_VOLUME_USD:
        return "emerging"
    return "not_eligible"


def record_universe_snapshot(conn, observed_at: datetime, contracts: list[dict], selected_symbols: set[str]):
    """Persist every eligible scanner contract before detailed filtering occurs."""
    rows = []
    for contract in contracts:
        symbol = contract["binance_symbol"]
        underlying = symbol.removesuffix("USDT")
        rows.append((
            observed_at,
            symbol,
            contract.get("coinalyze_symbol", f"{symbol}_PERP.A"),
            underlying,
            contract["vol_24h_usd"],
            contract["last_price"],
            classify_liquidity_tier(contract["vol_24h_usd"]),
            symbol in selected_symbols,
        ))

    conn.executemany("""
        INSERT OR REPLACE INTO universe_snapshots (
            observed_at, binance_symbol, coinalyze_symbol, underlying,
            volume_24h_usd, last_price, liquidity_tier, selected_for_scan
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)


def record_candidate(conn, candidate: dict) -> str:
    """Append a point-in-time candidate and return its immutable identifier."""
    required = {
        "observed_at", "asset", "setup_class", "phase", "strategy_id",
        "liquidity_tier", "status", "valid_until",
    }
    missing = required - candidate.keys()
    if missing:
        raise ValueError(f"Candidate missing required fields: {', '.join(sorted(missing))}")

    candidate_id = candidate.get("candidate_id")
    if not candidate_id:
        raise ValueError("Non-emitted candidates require an explicit stable candidate_id")
    conn.execute("""
        INSERT INTO alpha_candidates (
            candidate_id, observed_at, asset, source_symbol, direction,
            setup_class, phase, strategy_id, liquidity_tier, status,
            valid_until, entry_condition, invalidation_price, targets,
            feature_snapshot
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        candidate_id,
        candidate["observed_at"],
        candidate["asset"],
        candidate.get("source_symbol"),
        candidate.get("direction"),
        candidate["setup_class"],
        candidate["phase"],
        candidate["strategy_id"],
        candidate["liquidity_tier"],
        candidate["status"],
        candidate["valid_until"],
        json.dumps(candidate.get("entry_condition")),
        candidate.get("invalidation_price"),
        json.dumps(candidate.get("targets")),
        json.dumps(candidate.get("feature_snapshot", {}), sort_keys=True),
    ))
    return candidate_id
