"""Persistence helpers for point-in-time alpha research records."""

import json
from datetime import datetime
from uuid import uuid4

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

    candidate_id = candidate.get("candidate_id", str(uuid4()))
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


def record_outcome(conn, candidate_id: str, outcome: dict):
    """Record one evaluation outcome without modifying its source candidate."""
    required = {"evaluated_at", "outcome"}
    missing = required - outcome.keys()
    if missing:
        raise ValueError(f"Outcome missing required fields: {', '.join(sorted(missing))}")

    conn.execute("""
        INSERT INTO alpha_outcomes (
            candidate_id, evaluated_at, entry_at, entry_price, outcome, expiry_at,
            return_15m, return_1h, return_4h, max_favorable_excursion,
            max_adverse_excursion, estimated_cost, net_return, details
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        candidate_id,
        outcome["evaluated_at"],
        outcome.get("entry_at"),
        outcome.get("entry_price"),
        outcome["outcome"],
        outcome.get("expiry_at"),
        outcome.get("return_15m"),
        outcome.get("return_1h"),
        outcome.get("return_4h"),
        outcome.get("max_favorable_excursion"),
        outcome.get("max_adverse_excursion"),
        outcome.get("estimated_cost"),
        outcome.get("net_return"),
        json.dumps(outcome.get("details", {}), sort_keys=True),
    ))
