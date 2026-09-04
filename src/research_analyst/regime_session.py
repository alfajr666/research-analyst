"""Pre-evaluation regime/session gate and its separate observation store."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import config
from entry_policy import session_context
from regime_score import regime_score_for_asset
from regime_history import init_regime_history_schema


GATE_VERSION = "regime-session-gate-v3"
SCORE_VERSION = "regime-score-v3"
FAMILY_ACTIVATION_VERSION = "family-activation-v2"
_FAMILY_WEIGHT_KEYS = {
    "trend": "trend_weight",
    "mean_reversion": "mean_reversion_weight",
    "reversal": "reversal_weight",
}


def _utc(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timestamp(value: Any) -> str:
    return _utc(value).isoformat()


def _id(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(part) for part in parts).encode()).hexdigest()


def init_regime_db(db_path: str | Path | None = None) -> Path:
    """Create the regime-owned database without touching either service DB."""
    path = Path(db_path or config.REGIME_DB_PATH)
    conn = config.get_db_connection(db_path=path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS regime_scores (
                observation_id TEXT PRIMARY KEY,
                asset TEXT NOT NULL,
                cutoff_at TEXT NOT NULL,
                rotation_feed_id TEXT NOT NULL,
                score_version TEXT NOT NULL,
                status TEXT NOT NULL,
                trend_weight REAL,
                mean_reversion_weight REAL,
                reversal_weight REAL,
                confidence REAL,
                inputs_json TEXT NOT NULL,
                components_json TEXT NOT NULL,
                source_observation_ids TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                UNIQUE(asset, cutoff_at, rotation_feed_id, score_version)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS regime_gate_decisions (
                decision_id TEXT PRIMARY KEY,
                asset TEXT NOT NULL,
                cutoff_at TEXT NOT NULL,
                rotation_feed_id TEXT NOT NULL,
                gate_version TEXT NOT NULL,
                decision TEXT NOT NULL,
                session_name TEXT NOT NULL,
                session_phase TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                family_activation_json TEXT NOT NULL,
                score_observation_id TEXT,
                reversal_evidence_json TEXT NOT NULL DEFAULT '{}',
                recorded_at TEXT NOT NULL,
                UNIQUE(asset, cutoff_at, rotation_feed_id, gate_version)
            )
        """)
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(regime_gate_decisions)")
        }
        if "family_activation_json" not in columns:
            conn.execute(
                "ALTER TABLE regime_gate_decisions "
                "ADD COLUMN family_activation_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "reversal_evidence_json" not in columns:
            conn.execute(
                "ALTER TABLE regime_gate_decisions "
                "ADD COLUMN reversal_evidence_json TEXT NOT NULL DEFAULT '{}'"
            )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_regime_scores_cutoff ON regime_scores (cutoff_at, asset)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_regime_gate_cutoff ON regime_gate_decisions (cutoff_at, asset)")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(regime_scores)")}
        if "source_references_json" not in columns:
            conn.execute(
                "ALTER TABLE regime_scores ADD COLUMN source_references_json TEXT NOT NULL DEFAULT '{}'"
            )
        init_regime_history_schema(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def _family_activation(
    score: dict[str, Any],
    previous_score: dict[str, Any] | None = None,
    previous_activation: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    on_threshold = float(getattr(config, "REGIME_SESSION_FAMILY_ON_THRESHOLD", 0.35))
    off_threshold = float(getattr(config, "REGIME_SESSION_FAMILY_OFF_THRESHOLD", 0.25))
    activation = {}
    for family, weight_key in _FAMILY_WEIGHT_KEYS.items():
        if family == "reversal" and isinstance(score.get("reversal_gate"), dict):
            gate = score["reversal_gate"]
            activation[family] = {
                "active": bool(gate.get("active")),
                "weight": 1.0 if gate.get("active") else 0.0,
                "previous_weight": None,
                "reason": "reversal_gate_active" if gate.get("active") else "reversal_gate_blocked",
                "direction": gate.get("direction", "none"),
                "divergence_type": gate.get("divergence_type", "none"),
                "pivot_ids": gate.get("pivot_ids", []),
                "evidence": gate,
            }
            continue
        weight = float(score.get(weight_key) or 0.0)
        previous_weight = float((previous_score or {}).get(weight_key) or 0.0)
        was_active = bool((previous_activation or {}).get(family, {}).get("active"))
        if weight >= on_threshold:
            active = True
            reason = "on_threshold"
        elif (was_active or (previous_activation is None and previous_weight >= on_threshold)) and weight >= off_threshold:
            active = True
            reason = "hysteresis_hold"
        else:
            active = False
            reason = "below_threshold"
        activation[family] = {
            "active": active,
            "weight": weight,
            "previous_weight": previous_weight if previous_score else None,
            "reason": reason,
        }
    return activation


def gate_decision(
    asset: str,
    cutoff: Any,
    score: dict[str, Any],
    feed_id: str,
    previous_score: dict[str, Any] | None = None,
    previous_activation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the deterministic pre-evaluation decision for one asset."""
    session = session_context(cutoff)
    reasons = []
    if score.get("status") != "ok":
        reasons.append("regime_score_insufficient_data")
    if session["session_phase"] == "unknown":
        reasons.append("session_context_unavailable")
    if session["session_phase"] == "cooldown":
        reasons.append("session_open_cooldown")
    family_activation = _family_activation(score, previous_score, previous_activation)
    return {
        "asset": str(asset).upper(),
        "cutoff_at": _timestamp(cutoff),
        "rotation_feed_id": feed_id,
        "gate_version": GATE_VERSION,
        "decision": "block" if reasons else "allow",
        "session_name": session["session_name"],
        "session_phase": session["session_phase"],
        "reasons": reasons,
        "family_weights": {
            family: float(score.get(weight_key) or 0.0)
            for family, weight_key in _FAMILY_WEIGHT_KEYS.items()
        },
        "active_families": (
            [family for family, result in family_activation.items() if result["active"]]
            if not reasons else []
        ),
        "family_activation": {
            "version": FAMILY_ACTIVATION_VERSION,
            "threshold_on": float(getattr(config, "REGIME_SESSION_FAMILY_ON_THRESHOLD", 0.35)),
            "threshold_off": float(getattr(config, "REGIME_SESSION_FAMILY_OFF_THRESHOLD", 0.25)),
            "families": family_activation,
        },
        "reversal_evidence": score.get("reversal_gate", {}),
    }


def _insert_observation(
    conn,
    asset: str,
    cutoff: Any,
    feed_id: str,
    score: dict[str, Any],
    gate: dict[str, Any],
    recorded_at: str,
) -> None:
    cutoff_text = _timestamp(cutoff)
    observation_id = _id(asset, cutoff_text, feed_id, SCORE_VERSION)
    decision_id = _id(asset, cutoff_text, feed_id, GATE_VERSION)
    market_data = score.get("market_data", {})
    conn.execute(
        """
        INSERT OR IGNORE INTO regime_scores (
            observation_id, asset, cutoff_at, rotation_feed_id, score_version,
            status, trend_weight, mean_reversion_weight, reversal_weight,
            confidence, inputs_json, components_json, source_observation_ids,
            source_references_json, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            observation_id,
            str(asset).upper(),
            cutoff_text,
            feed_id,
            SCORE_VERSION,
            "ready" if score.get("status") == "ok" else "insufficient_data",
            score.get("trend_weight"),
            score.get("mean_reversion_weight"),
            score.get("reversal_weight"),
            score.get("confidence"),
            json.dumps(market_data, sort_keys=True, default=str),
            json.dumps(score.get("components", {}), sort_keys=True, default=str),
            json.dumps(score.get("source_observation_ids", []), sort_keys=True),
            json.dumps(score.get("source_references", {}), sort_keys=True, default=str),
            recorded_at,
        ),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO regime_gate_decisions (
            decision_id, asset, cutoff_at, rotation_feed_id, gate_version,
            decision, session_name, session_phase, reasons_json,
            family_activation_json, score_observation_id, reversal_evidence_json, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            decision_id,
            str(asset).upper(),
            cutoff_text,
            feed_id,
            GATE_VERSION,
            gate["decision"],
            gate["session_name"],
            gate["session_phase"],
            json.dumps(gate["reasons"], sort_keys=True),
            json.dumps(gate["family_activation"], sort_keys=True),
            observation_id,
            json.dumps(gate.get("reversal_evidence", {}), sort_keys=True, default=str),
            recorded_at,
        ),
    )


def _previous_score(conn: Any, asset: str, cutoff: Any) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT trend_weight, mean_reversion_weight, reversal_weight
          FROM regime_scores
         WHERE asset = ? AND cutoff_at < ? AND score_version = ?
         ORDER BY cutoff_at DESC
         LIMIT 1
        """,
        (str(asset).upper(), _timestamp(cutoff), SCORE_VERSION),
    ).fetchone()
    if row is None:
        return None
    return dict(zip(_FAMILY_WEIGHT_KEYS.values(), row))


def _previous_activation(conn: Any, asset: str, cutoff: Any) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT family_activation_json
          FROM regime_gate_decisions
         WHERE asset = ? AND cutoff_at < ? AND gate_version = ?
         ORDER BY cutoff_at DESC
         LIMIT 1
        """,
        (str(asset).upper(), _timestamp(cutoff), GATE_VERSION),
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row[0] or "{}").get("families", {})
    except (TypeError, ValueError):
        return None


def publish_regime_batch(
    cutoff: Any,
    *,
    assets: Iterable[str] | None = None,
    feed_id: str | None = None,
    market_db_path: str | Path | None = None,
    regime_db_path: str | Path | None = None,
    history_fetcher: Any | None = None,
    history_1h_fetcher: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Score and persist one rotation snapshot without writing market data."""
    cutoff = _utc(cutoff)
    if assets is None:
        from symbol_rotation import subscription_assets
        selected, feed = subscription_assets(cutoff)
        assets = selected
        feed_id = feed_id or str(feed.get("feed_id") or "unknown")
    assets = sorted({str(asset).upper() for asset in assets if str(asset).strip()})
    feed_id = feed_id or "manual"
    recorded_at = _utc(now or datetime.now(timezone.utc)).isoformat()
    init_regime_db(regime_db_path)
    market_conn = config.get_db_connection(read_only=True, db_path=market_db_path or config.MARKET_DB_PATH)
    regime_conn = config.get_db_connection(db_path=regime_db_path or config.REGIME_DB_PATH)
    summary = {
        "cutoff_at": cutoff.isoformat(),
        "feed_id": feed_id,
        "assets": assets,
        "allowed": [],
        "blocked": [],
        "family_assets": {family: [] for family in _FAMILY_WEIGHT_KEYS},
        "bootstrap": {},
        "asset_observations": {},
    }
    try:
        for asset in assets:
            try:
                score = regime_score_for_asset(
                    market_conn,
                    asset,
                    cutoff,
                    regime_conn=regime_conn,
                    history_fetcher=history_fetcher,
                    history_1h_fetcher=history_1h_fetcher,
                )
            except Exception as exc:
                score = {
                    "status": "insufficient_data",
                    "components": {"error": type(exc).__name__},
                    "market_data": {},
                }
            gate = gate_decision(
                asset,
                cutoff,
                score,
                feed_id,
                previous_score=_previous_score(regime_conn, asset, cutoff),
                previous_activation=_previous_activation(regime_conn, asset, cutoff),
            )
            _insert_observation(regime_conn, asset, cutoff, feed_id, score, gate, recorded_at)
            summary["bootstrap"][asset] = score.get(
                "regime_history", {"status": "unknown"}
            )
            history = score.get("regime_history", {})
            history_1h = history.get("1h", {}) if isinstance(history, dict) else {}
            history_4h = history.get("4h", {}) if isinstance(history, dict) else {}
            components = score.get("components", {})
            summary["asset_observations"][asset] = {
                "history_1h": {
                    "status": history_1h.get("status", "unknown"),
                    "covered_bars": history_1h.get("covered_bars"),
                    "missing_bars": history_1h.get("missing_bars"),
                    "last_error": history_1h.get("last_error"),
                    "next_retry_at": history_1h.get("next_retry_at"),
                },
                "history_4h": {
                    "status": history_4h.get("status", "unknown"),
                    "covered_bars": history_4h.get("covered_bars"),
                    "missing_bars": history_4h.get("missing_bars"),
                    "last_error": history_4h.get("last_error"),
                    "next_retry_at": history_4h.get("next_retry_at"),
                },
                "market_5m_bars": score.get("market_5m_bars"),
                "score_status": score.get("status", "unknown"),
                "missing_inputs": components.get("missing_inputs", []),
                "decision": gate["decision"],
                "gate_reasons": gate["reasons"],
                "active_families": gate["active_families"],
            }
            summary["blocked" if gate["decision"] == "block" else "allowed"].append(asset)
            if gate["decision"] == "allow":
                for family in gate["active_families"]:
                    summary["family_assets"][family].append(asset)
        regime_conn.commit()
    finally:
        market_conn.close()
        regime_conn.close()
    return summary


def format_regime_batch_log(summary: dict[str, Any]) -> str:
    """Format a compact operational summary without hiding per-asset reasons."""
    observations = summary.get("asset_observations", {})
    history_1h_ready = sum(
        value.get("history_1h", {}).get("status") == "ready"
        for value in observations.values()
    )
    history_4h_ready = sum(
        value.get("history_4h", {}).get("status") == "ready"
        for value in observations.values()
    )
    score_ready = sum(value.get("score_status") == "ok" for value in observations.values())
    diagnostics = {}
    for asset, value in observations.items():
        if value.get("decision") == "block" or value.get("score_status") != "ok":
            diagnostics[asset] = {
                "1h": value.get("history_1h"),
                "4h": value.get("history_4h"),
                "market_5m_bars": value.get("market_5m_bars"),
                "score": value.get("score_status"),
                "missing_inputs": value.get("missing_inputs", []),
                "decision": value.get("decision"),
                "reasons": value.get("gate_reasons", []),
                "families": value.get("active_families", []),
            }
    return json.dumps({
        "cutoff_at": summary["cutoff_at"],
        "feed_id": summary["feed_id"],
        "mode": config.REGIME_SESSION_MODE,
        "assets": len(observations),
        "history_1h_ready": history_1h_ready,
        "history_1h_retryable": len(observations) - history_1h_ready,
        "history_4h_ready": history_4h_ready,
        "history_4h_retryable": len(observations) - history_4h_ready,
        "score_ready": score_ready,
        "score_insufficient": len(observations) - score_ready,
        "gate_allow": len(summary["allowed"]),
        "gate_block": len(summary["blocked"]),
        "family_assets": {
            family: len(assets) for family, assets in summary["family_assets"].items()
        },
        "diagnostics": diagnostics,
    }, sort_keys=True, default=str)


def load_gate_scope(
    assets: Iterable[str],
    cutoff: Any,
    feed_id: str,
    *,
    mode: str | None = None,
    regime_db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Load exact-cutoff gate results for the evaluator's asset scope."""
    assets = sorted({str(asset).upper() for asset in assets if str(asset).strip()})
    mode = str(mode or config.REGIME_SESSION_MODE).strip().lower()
    if mode not in {"off", "shadow", "enforce"}:
        raise ValueError("regime session mode must be off, shadow, or enforce")
    scope = {
        "mode": mode,
        "feed_id": feed_id,
        "cutoff_at": _timestamp(cutoff),
        "allowed_assets": list(assets),
        "blocked_assets": [],
        "missing_assets": [],
        "would_block_assets": [],
        "decisions": {},
        "family_assets": {family: [] for family in _FAMILY_WEIGHT_KEYS},
    }
    if mode == "off":
        return scope
    try:
        conn = config.get_db_connection(read_only=True, db_path=regime_db_path or config.REGIME_DB_PATH)
    except Exception:
        scope["missing_assets"] = list(assets)
        if mode == "enforce":
            scope["allowed_assets"] = []
        return scope
    try:
        for asset in assets:
            row = conn.execute(
                """
                SELECT g.decision, g.session_name, g.session_phase, g.reasons_json,
                        g.family_activation_json, g.reversal_evidence_json,
                        s.trend_weight, s.mean_reversion_weight, s.reversal_weight
                  FROM regime_gate_decisions AS g
                  LEFT JOIN regime_scores AS s
                    ON s.observation_id = g.score_observation_id
                 WHERE g.asset = ? AND g.cutoff_at = ? AND g.rotation_feed_id = ?
                    AND g.gate_version = ?
                """,
                (asset, scope["cutoff_at"], feed_id, GATE_VERSION),
            ).fetchone()
            if row is None:
                scope["missing_assets"].append(asset)
                continue
            reasons = json.loads(row[3] or "[]")
            family_activation = json.loads(row[4] or "{}")
            if not family_activation.get("families"):
                score = dict(zip(_FAMILY_WEIGHT_KEYS.values(), row[6:9]))
                family_activation = {
                    "version": FAMILY_ACTIVATION_VERSION,
                    "threshold_on": float(getattr(config, "REGIME_SESSION_FAMILY_ON_THRESHOLD", 0.35)),
                    "threshold_off": float(getattr(config, "REGIME_SESSION_FAMILY_OFF_THRESHOLD", 0.25)),
                    "families": _family_activation(
                        score,
                        _previous_score(conn, asset, cutoff),
                        _previous_activation(conn, asset, cutoff),
                    ),
                }
            active_families = (
                [family for family, result in family_activation.get("families", {}).items()
                 if result.get("active")]
                if row[0] == "allow" else []
            )
            scope["decisions"][asset] = {
                "decision": row[0],
                "session_name": row[1],
                "session_phase": row[2],
                "reasons": reasons,
                "family_weights": {
                    family: float(weight or 0.0)
                    for family, weight in zip(_FAMILY_WEIGHT_KEYS, row[6:9])
                },
                "family_activation": family_activation,
                "reversal_evidence": json.loads(row[5] or "{}"),
                "active_families": active_families,
            }
            if row[0] == "block":
                scope["blocked_assets"].append(asset)
                scope["would_block_assets"].append(asset)
            for family in active_families:
                scope["family_assets"][family].append(asset)
    finally:
        conn.close()

    if mode == "enforce":
        scope["allowed_assets"] = [
            asset for asset in assets if scope["decisions"].get(asset, {}).get("decision") == "allow"
        ]
    return scope


def wait_for_gate_scope(
    assets: Iterable[str],
    cutoff: Any,
    feed_id: str,
    *,
    mode: str | None = None,
    regime_db_path: str | Path | None = None,
    grace_seconds: int | None = None,
) -> dict[str, Any]:
    """Wait briefly for the exact cutoff without bypassing enforcement."""
    mode = str(mode or config.REGIME_SESSION_MODE).strip().lower()
    if mode != "enforce":
        return load_gate_scope(
            assets, cutoff, feed_id, mode=mode, regime_db_path=regime_db_path
        )
    deadline = time.monotonic() + int(
        config.REGIME_SESSION_GRACE_SECONDS if grace_seconds is None else grace_seconds
    )
    scope = load_gate_scope(
        assets, cutoff, feed_id, mode=mode, regime_db_path=regime_db_path
    )
    while scope["missing_assets"] and time.monotonic() < deadline:
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
        scope = load_gate_scope(
            assets, cutoff, feed_id, mode=mode, regime_db_path=regime_db_path
        )
    return scope


def main() -> None:
    """Run one batch or the managed completed-5m regime worker."""
    import argparse

    parser = argparse.ArgumentParser(description="Per-asset regime-session worker")
    parser.add_argument("--once", action="store_true", help="Publish one completed 5m cutoff")
    parser.add_argument("--cutoff", help="UTC ISO cutoff for one-shot replay")
    args = parser.parse_args()
    from strategy_v2_context import completed_cycle_for

    if args.once or args.cutoff:
        cutoff = _utc(args.cutoff) if args.cutoff else completed_cycle_for(datetime.now(timezone.utc), "5m")
        print(format_regime_batch_log(publish_regime_batch(cutoff)))
        return

    last_cutoff = None
    while True:
        cutoff = completed_cycle_for(datetime.now(timezone.utc), "5m")
        if cutoff != last_cutoff:
            print(format_regime_batch_log(publish_regime_batch(cutoff)), flush=True)
            last_cutoff = cutoff
        time.sleep(1.0)


if __name__ == "__main__":
    main()
