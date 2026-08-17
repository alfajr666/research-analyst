"""Emit newly entered accumulation-base alpha events from local research data."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from accumulation_detection import check_accumulation, completed_cycle, confluence, get_hourly_buckets
from alpha_outbox import write_event


EVALUATOR_INTERVAL_SECONDS = 15 * 60
SCANNER_MAX_AGE = timedelta(minutes=75)
STATE_FILE = config.DEFAULT_DB_DIR / "accumulation_evaluator_state.json"
LEGACY_STATE_FILE = config.DEFAULT_DB_DIR / "accumulation_state.json"
PENDING_FILE = config.DEFAULT_DB_DIR / "scanner_pending_accums.json"


def _clamp(value: float) -> float:
    return max(0.0, min(value, 1.0))


def confidence_from_setup(accumulation: dict, setup: dict) -> tuple[float, dict[str, float]]:
    """Score the strength of a qualifying setup from its observed market inputs."""
    volume = _clamp((float(accumulation["vol_spike"]) - 1.5) / 1.0)
    quietness = _clamp(1.0 - abs(float(accumulation["price_change_1h"])) / 3.0)
    ema_proximity = _clamp(1.0 - float(setup["ema_distance"]) / 0.01)
    candle_body = abs(float(setup["close"]) - float(setup["open"])) / float(setup["close"])
    candle_strength = _clamp(candle_body / 0.005)
    hourly_move = float(accumulation["price_change_1h"])
    direction = setup["direction"]
    directional_alignment = 1.0 if (direction == "long" and hourly_move >= 0) or (direction == "short" and hourly_move <= 0) else 0.0
    components = {
        "volume": round(volume * 0.35, 4),
        "quietness": round(quietness * 0.20, 4),
        "ema_proximity": round(ema_proximity * 0.25, 4),
        "candle_strength": round(candle_strength * 0.15, 4),
        "directional_alignment": round(directional_alignment * 0.05, 4),
    }
    return round(sum(components.values()), 4), components


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        return None


def load_state(path: Path = STATE_FILE) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return state if isinstance(state.get("active"), dict) else {"active": {}}
    except (OSError, ValueError, json.JSONDecodeError):
        # Carry forward legacy alert suppression once without allowing the old
        # monitor to own or mutate this evaluator's state thereafter.
        if path == STATE_FILE:
            try:
                legacy = json.loads(LEGACY_STATE_FILE.read_text(encoding="utf-8"))
                return {"active": {
                    symbol: {"entered_at": details.get("first_detected"), "source": details.get("source", "duckdb"),
                             "observed_at": details.get("last_alerted")}
                    for symbol, details in legacy.get("alerted", {}).items()
                }}
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        return {"active": {}}


def save_state(state: dict, path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def fresh_scanner_symbols(path: Path, now: datetime) -> dict[str, dict]:
    """Read, but never consume, the most recent scanner handoff if it is fresh."""
    try:
        pending = json.loads(path.read_text(encoding="utf-8"))
        timestamp = _parse_timestamp(pending.get("scanner_timestamp"))
        if timestamp is None or now - timestamp > SCANNER_MAX_AGE or timestamp > now + timedelta(minutes=5):
            return {}
        return pending.get("symbols", {}) if isinstance(pending.get("symbols"), dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def event_from_setup(asset: str, symbol: str, source: str, accumulation: dict, setup: dict) -> dict:
    """Translate a completed-bar confluence into a conservative portable event."""
    direction = setup["direction"]
    confidence, confidence_components = confidence_from_setup(accumulation, setup)
    ema = setup["ema_99"]
    close = setup["close"]
    entry = max(ema, close) if direction == "long" else min(ema, close)
    invalidation = ema * (0.985 if direction == "long" else 1.015)
    risk = abs(entry - invalidation)
    target = entry + risk * (1.5 if direction == "long" else -1.5)
    observed_at = setup["bar_timestamp"]
    return {
        "schema_version": 1,
        "strategy_id": "accumulation-base-v1",
        "asset": asset,
        "direction": direction,
        "setup_class": "accumulation_base",
        "phase": "confirmed_pullback",
        "observed_at": observed_at.isoformat(),
        "valid_until": (observed_at + timedelta(hours=4)).isoformat(),
        "horizon_minutes": 240,
        "confidence": confidence,
        "entry_condition": {"type": "limit_at_ema_context", "price": round(entry, 8)},
        "invalidation_price": round(invalidation, 8),
        "targets": [round(target, 8)],
        "feature_snapshot": {
            "source": source,
            "source_symbol": symbol,
            "volume_spike_multiple": accumulation["vol_spike"],
            "quiet_price_change_1h_pct": accumulation["price_change_1h"],
            "hour_volume": accumulation.get("hour_volume"),
            "ema_99": round(ema, 8),
            "ema_distance_pct": round(setup["ema_distance"] * 100, 4),
            "completed_bar_at": observed_at.isoformat(),
            "execution_candle": "green" if direction == "long" else "red",
            "close": close,
            "confidence_components": confidence_components,
        },
    }


def _scanner_accumulation(meta: dict) -> dict | None:
    try:
        return {
            "vol_spike": round(float(meta["vol_spike"]), 2),
            "price_change_1h": round(float(meta["price_change_1h"]), 2),
            "hour_volume": None,
        }
    except (KeyError, TypeError, ValueError):
        return None


def evaluate(conn, now: datetime, pending_path: Path = PENDING_FILE) -> dict[str, dict]:
    """Return current confirmed setups, preferring the fresh scanner source."""
    cutoff = completed_cycle(now)
    setups: dict[str, dict] = {}
    scanner_symbols = fresh_scanner_symbols(pending_path, now)
    for symbol, meta in scanner_symbols.items():
        accumulation = _scanner_accumulation(meta)
        confirmed = confluence(conn, symbol, cutoff)
        if accumulation and confirmed:
            setups[symbol] = {"asset": meta.get("underlying", symbol.split("_")[0]), "source": "scanner",
                              "accumulation": accumulation, "setup": confirmed}
    rows = conn.execute("""
        SELECT DISTINCT symbol, underlying FROM futures_data
        WHERE timestamp < ? AND timestamp >= ? - INTERVAL '28 hours'
    """, (cutoff, cutoff)).fetchall()
    for symbol, asset in rows:
        if symbol in setups:
            continue
        accumulation = check_accumulation(get_hourly_buckets(conn, symbol, cutoff))
        confirmed = confluence(conn, symbol, cutoff)
        if accumulation and confirmed:
            setups[symbol] = {"asset": asset, "source": "duckdb", "accumulation": accumulation, "setup": confirmed}
    return setups


def run_once(now: datetime | None = None, state_path: Path = STATE_FILE, pending_path: Path = PENDING_FILE,
             outbox_dir=None) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    conn = config.get_db_connection(read_only=True)
    try:
        current = evaluate(conn, now, pending_path)
    finally:
        conn.close()
    state = load_state(state_path)
    active = state["active"]
    emitted = 0
    entered = 0
    for symbol, candidate in current.items():
        if symbol in active:
            continue
        entered += 1
        event = event_from_setup(candidate["asset"], symbol, candidate["source"], candidate["accumulation"], candidate["setup"])
        created, path = write_event(event, outbox_dir) if outbox_dir else write_event(event)
        print(f"{'Emitted' if created else 'Deduplicated'} accumulation event: {path.name}")
        active[symbol] = {"entered_at": now.isoformat(), "source": candidate["source"],
                          "observed_at": event["observed_at"]}
        emitted += int(created)
    exited = set(active) - set(current)
    for symbol in exited:
        del active[symbol]
    state["last_check"] = now.isoformat()
    save_state(state, state_path)
    return {"active": len(active), "entered": entered, "emitted": emitted, "exited": len(exited)}


def main() -> None:
    while True:
        try:
            print(f"Accumulation evaluator: {run_once()}")
        except Exception as error:
            print(f"Accumulation evaluator error: {error}", file=sys.stderr)
        time.sleep(EVALUATOR_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
