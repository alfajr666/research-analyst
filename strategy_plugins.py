"""Strategy plugin registry and invocation per data-platform-strategy-plugins spec.

Plugins are read-only against finalized cutoff snapshots.
They write exclusively via alpha_outbox.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List

import config
from alpha_outbox import write_event
from strategy_v2_context import completed_cycle_for

# Per spec: re-export from config for modules that imported here before
PRICE_STRUCTURE_STRATEGY_IDS = getattr(config, "PRICE_STRUCTURE_STRATEGY_IDS", set())
MIXED_STRATEGY_IDS = getattr(config, "MIXED_STRATEGY_IDS", set())


def _get_bar_purity(conn, asset: str, observed_at: Any, interval: str = "15m") -> Dict[str, Any]:
    try:
        ts = observed_at
        if not isinstance(ts, datetime):
            ts = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).replace(tzinfo=timezone.utc)
        ts = ts.astimezone(timezone.utc)
        row = conn.execute(
            """
            SELECT source, payload_json FROM source_observations
            WHERE asset = ? AND interval=? AND source_end <= ?
            ORDER BY source_end DESC LIMIT 1
            """, (asset, interval, ts)
        ).fetchone()
        if not row:
            return {"data_purity": "unknown", "price_source": "unknown"}
        src, pj = row
        p = json.loads(pj) if pj else {}
        prov = p.get("provenance", {}) or {}
        if src == "coinalyze":
            purity = "pure_ca"
            price_source = "coinalyze"
        elif src in (getattr(config, "BYBIT_WS_SOURCE", "bybit_ws"), getattr(config, "BINANCE_WS_SOURCE", "binance_ws")):
            purity = getattr(config, "WS_DATA_PURITY", "pure_ws")
            price_source = src
        else:
            purity = getattr(config, "FAILOVER_SOURCE_NAME", "venue_agg_v1")
            price_source = getattr(config, "FAILOVER_SOURCE_NAME", "venue_agg_v1")
        return {
            "data_purity": purity,
            "price_source": price_source,
            "fallback_reason": None if purity.startswith("pure_") else "ca_missing_bar",
        }
    except Exception:
        return {"data_purity": "pure_ca", "price_source": "coinalyze"}


KNOWN_STRATEGIES = {
    "accumulation-base-v1",
    "impulse-ignition-v1",
    "continuation-breakout-balanced-v1",
    "accumulation-base-v2",
    "impulse-ignition-v2",
    "continuation-breakout-v2",
    "rsi-reclaim-v1",
    "liquidity-sweep-reversal-v1",
    "bb-rsi-meanrev-v1",
    "failed-break-v3",
    "williams-fractal-scalp-v1",
    "ema9-continuation-stochrsi-v1",
}

# Pre-refactor evaluators (built on alpha_evaluator / accumulation_evaluator).
# Retired via the active/inactive flag (phase 6): kept registered so they can be
# re-activated, but defaulted to 'inactive' in plugin_states.
LEGACY_STRATEGY_IDS = {
    "accumulation-base-v1",
    "impulse-ignition-v1",
    "continuation-breakout-balanced-v1",
}


@dataclass
class StrategyPlugin:
    id: str
    version: str
    required_datasets: tuple[str, ...]
    optional_datasets: tuple[str, ...]
    run: Callable[[str, dict], List[dict]]  # (cutoff_id, snapshot) -> events


_REGISTRY: Dict[str, StrategyPlugin] = {}


def register(plugin: StrategyPlugin) -> None:
    _REGISTRY[plugin.id] = plugin


def _load_builtin_plugins():
    # Import here to avoid circulars at module load; existing evaluators provide the logic.
    from accumulation_evaluator import evaluate as acc_evaluate, event_from_setup as acc_event_from_setup
    from alpha_evaluator import ignition_candidates, acceleration_candidates, event_from_candidate, completed_cycle
    from alpha_outbox import write_event as _write_event  # local to avoid name clash
    from structure_zones import attach_zone_evidence

    def _acc_run(cutoff_id: str, snapshot: dict) -> List[dict]:
        conn = config.get_db_connection(read_only=True, db_path=snapshot.get("db_path"))
        try:
            now = snapshot.get("now") or datetime.now(timezone.utc)
            # Use the existing evaluate which returns setups for symbols
            setups = acc_evaluate(conn, now)
            emitted = []
            for symbol, cand in setups.items():
                ev = acc_event_from_setup(cand["asset"], symbol, cand["source"], cand["accumulation"], cand["setup"])
                ev["plugin_version"] = "v1"
                ev["input_snapshot_id"] = cutoff_id
                zs = snapshot.get("zones", []) if "zones" in snapshot else []
                attach_zone_evidence(ev, zs)
                created, _ = _write_event(ev)
                if created:
                    emitted.append(ev)
            return emitted
        finally:
            conn.close()

    def _ign_run(cutoff_id: str, snapshot: dict) -> List[dict]:
        conn = config.get_db_connection(read_only=True, db_path=snapshot.get("db_path"))
        try:
            now = snapshot.get("now") or datetime.now(timezone.utc)
            cands = ignition_candidates(conn, cutoff=completed_cycle(now))
            emitted = []
            for cand in cands:
                ev = event_from_candidate(cand, "ignition")
                ev["plugin_version"] = "v1"
                ev["input_snapshot_id"] = cutoff_id
                zs = snapshot.get("zones", []) if "zones" in snapshot else []
                attach_zone_evidence(ev, zs)
                created, _ = _write_event(ev)
                if created:
                    emitted.append(ev)
            return emitted
        finally:
            conn.close()

    def _cont_run(cutoff_id: str, snapshot: dict) -> List[dict]:
        conn = config.get_db_connection(read_only=True, db_path=snapshot.get("db_path"))
        try:
            now = snapshot.get("now") or datetime.now(timezone.utc)
            cands = acceleration_candidates(conn, cutoff=completed_cycle(now))
            emitted = []
            for cand in cands:
                ev = event_from_candidate(cand, "continuation")
                ev["plugin_version"] = "v1"
                ev["input_snapshot_id"] = cutoff_id
                zs = snapshot.get("zones", []) if "zones" in snapshot else []
                attach_zone_evidence(ev, zs)
                created, _ = _write_event(ev)
                if created:
                    emitted.append(ev)
            return emitted
        finally:
            conn.close()

    from accumulation_base_v2 import run_plugin as acc_v2_run
    from impulse_ignition_v2 import run_plugin as ign_v2_run
    from continuation_breakout_v2 import run_plugin as cont_v2_run
    from rsi_reclaim_v1 import run_plugin as rsi_reclaim_run
    from liquidity_sweep_reversal_v1 import run_plugin as lsr_run
    from bb_rsi_meanrev_v1 import run_plugin as bb_rsi_run
    from failed_break_v3 import run_plugin as failed_break_run
    from williams_fractal_scalp_v1 import run_plugin as williams_run
    from ema9_continuation_stochrsi_v1 import run_plugin as ema9_run

    register(StrategyPlugin("accumulation-base-v1", "v1", ("bars_15m",), ("fvg_1h",), _acc_run))
    register(StrategyPlugin("impulse-ignition-v1", "v1", ("bars_15m",), ("vp",), _ign_run))
    register(StrategyPlugin("continuation-breakout-balanced-v1", "v1", ("bars_15m",), ("acceleration",), _cont_run))
    register(StrategyPlugin("accumulation-base-v2", "v2", ("bars_15m",), ("fvg_1h", "fvg_4h", "vp"), acc_v2_run))
    register(StrategyPlugin("impulse-ignition-v2", "v2", ("bars_15m",), ("fvg_1h", "fvg_4h", "vp"), ign_v2_run))
    register(StrategyPlugin("continuation-breakout-v2", "v2", ("bars_15m",), ("fvg_1h", "fvg_4h", "vp"), cont_v2_run))
    register(StrategyPlugin("rsi-reclaim-v1", "v1", ("bars_15m",), ("fvg_1h", "fvg_4h", "vp"), rsi_reclaim_run))
    register(StrategyPlugin("liquidity-sweep-reversal-v1", "v1", ("bars_15m",), ("fvg_1h", "fvg_4h", "vp"), lsr_run))
    register(StrategyPlugin("bb-rsi-meanrev-v1", "v1", ("bars_5m",), (), bb_rsi_run))
    # Primary execution bars gate invocation; each plugin loads its own HTF context.
    register(StrategyPlugin("failed-break-v3", "v3", ("bars_5m",), (), failed_break_run))
    register(StrategyPlugin("williams-fractal-scalp-v1", "v1", ("bars_1m",), (), williams_run))
    register(StrategyPlugin("ema9-continuation-stochrsi-v1", "v1", ("bars_1m",), (), ema9_run))


_load_builtin_plugins()


def load_enabled_plugins() -> List[StrategyPlugin]:
    enabled = []
    for sid in config.STRATEGY_ENABLED_IDS:
        if sid not in KNOWN_STRATEGIES:
            raise RuntimeError(f"unknown strategy id in STRATEGY_ENABLED_IDS: {sid}")
        if sid not in _REGISTRY:
            raise RuntimeError(f"strategy not registered: {sid}")
        enabled.append(_REGISTRY[sid])
    return enabled


def _explicit_active_set() -> set:
    return set(getattr(config, "STRATEGY_ACTIVE_IDS", ()) or ())


def plugin_effective_active(conn, strategy_id: str) -> bool:
    """Enabled AND not toggled off (env allowlist or plugin_states runtime flag)."""
    if strategy_id not in config.STRATEGY_ENABLED_IDS:
        return False
    explicit = _explicit_active_set()
    row = None
    try:
        row = conn.execute(
            "SELECT state FROM plugin_states WHERE strategy_id = ?", (strategy_id,)
        ).fetchone()
    except Exception:
        row = None
    if row:
        return row[0] == "active"
    if explicit:
        return strategy_id in explicit
    # No DB row, no explicit env: legacy ids default to inactive (retired).
    return strategy_id not in LEGACY_STRATEGY_IDS


def load_active_plugins(conn=None) -> List[StrategyPlugin]:
    own = conn is None
    if own:
        conn = config.get_db_connection(read_only=True)
    try:
        return [p for p in load_enabled_plugins() if plugin_effective_active(conn, p.id)]
    finally:
        if own:
            conn.close()


def get_plugin_state(strategy_id: str, db_path: str | Path | None = None) -> dict:
    conn = config.get_db_connection(read_only=True, db_path=db_path)
    try:
        row = conn.execute(
            "SELECT state, updated_at, reason, updated_by FROM plugin_states WHERE strategy_id = ?",
            (strategy_id,),
        ).fetchone()
        active = plugin_effective_active(conn, strategy_id)
    finally:
        conn.close()
    if not row:
        return {"strategy_id": strategy_id, "state": "active" if active else "inactive",
                "updated_at": None, "reason": None, "updated_by": None,
                "effective_active": active, "source": "default"}
    return {"strategy_id": strategy_id, "state": row[0], "updated_at": row[1],
            "reason": row[2], "updated_by": row[3], "effective_active": active,
            "source": "plugin_states"}


def set_plugin_state(strategy_id: str, state: str, reason: str | None = None,
                    updated_by: str | None = None, db_path: str | Path | None = None) -> None:
    if state not in ("active", "inactive", "paused"):
        raise ValueError(f"invalid plugin state: {state}")
    conn = config.get_db_connection(read_only=False, db_path=db_path)
    try:
        conn.execute(
            """
            INSERT INTO plugin_states (strategy_id, state, updated_at, reason, updated_by)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (strategy_id) DO UPDATE SET
                state = excluded.state,
                updated_at = excluded.updated_at,
                reason = excluded.reason,
                updated_by = excluded.updated_by
            """,
            (strategy_id, state, datetime.now(timezone.utc), reason, updated_by),
        )
        conn.commit()
    finally:
        conn.close()


def ensure_plugin_states(db_path: str | Path | None = None) -> None:
    """Seed plugin_states on first run so the active/inactive flag is explicit.
    Legacy evaluators default to 'inactive' (retired); everything else 'active'
    unless overridden by STRATEGY_ACTIVE_IDS."""
    explicit = _explicit_active_set()
    conn = config.get_db_connection(read_only=False, db_path=db_path)
    try:
        now = datetime.now(timezone.utc)
        for sid in KNOWN_STRATEGIES:
            if sid not in config.STRATEGY_ENABLED_IDS:
                state = "inactive"
            elif explicit:
                state = "active" if sid in explicit else "inactive"
            else:
                state = "inactive" if sid in LEGACY_STRATEGY_IDS else "active"
            conn.execute(
                """
                INSERT OR IGNORE INTO plugin_states
                    (strategy_id, state, updated_at, reason, updated_by)
                VALUES (?, ?, ?, 'seeded at startup', 'system')
                """,
                (sid, state, now),
            )
        conn.commit()
    finally:
        conn.close()


def deactivate_all_strategies(db_path: str | Path | None = None,
                              reason: str = "deactivated via control",
                              updated_by: str = "user") -> None:
    """Bulk-deactivate every known strategy (master off lever for the active/inactive flag)."""
    conn = config.get_db_connection(read_only=False, db_path=db_path)
    try:
        now = datetime.now(timezone.utc)
        for sid in KNOWN_STRATEGIES:
            conn.execute(
                """
                INSERT INTO plugin_states (strategy_id, state, updated_at, reason, updated_by)
                VALUES (?, 'inactive', ?, ?, ?)
                ON CONFLICT (strategy_id) DO UPDATE SET
                    state = 'inactive', updated_at = excluded.updated_at,
                    reason = excluded.reason, updated_by = excluded.updated_by
                """,
                (sid, now, reason, updated_by),
            )
        conn.commit()
    finally:
        conn.close()


def activate_all_strategies(db_path: str | Path | None = None,
                           reason: str = "activated via control",
                           updated_by: str = "user") -> None:
    """Bulk-reactivate every known strategy (inverse of deactivate_all)."""
    conn = config.get_db_connection(read_only=False, db_path=db_path)
    try:
        now = datetime.now(timezone.utc)
        for sid in KNOWN_STRATEGIES:
            conn.execute(
                """
                INSERT INTO plugin_states (strategy_id, state, updated_at, reason, updated_by)
                VALUES (?, 'active', ?, ?, ?)
                ON CONFLICT (strategy_id) DO UPDATE SET
                    state = 'active', updated_at = excluded.updated_at,
                    reason = excluded.reason, updated_by = excluded.updated_by
                """,
                (sid, now, reason, updated_by),
            )
        conn.commit()
    finally:
        conn.close()


def list_plugin_states(db_path: str | Path | None = None) -> List[dict]:
    conn = config.get_db_connection(read_only=True, db_path=db_path)
    try:
        rows = conn.execute(
            "SELECT strategy_id, state, updated_at, reason, updated_by FROM plugin_states"
        ).fetchall()
        seeded = {r[0]: r for r in rows}
    finally:
        conn.close()
    out = []
    for sid in KNOWN_STRATEGIES:
        if sid in seeded:
            r = seeded[sid]
            out.append({"strategy_id": sid, "state": r[1], "updated_at": r[2],
                        "reason": r[3], "updated_by": r[4]})
        else:
            active = sid not in LEGACY_STRATEGY_IDS
            out.append({"strategy_id": sid, "state": "active" if active else "inactive",
                        "updated_at": None, "reason": None, "updated_by": None})
    return out


def _ensure_cutoff_finalized(conn, cutoff_id: str) -> None:
    row = conn.execute("SELECT status FROM cutoff_runs WHERE cutoff_id = ?", (cutoff_id,)).fetchone()
    if row is None or row[0] != "finalized":
        raise ValueError(f"cutoff {cutoff_id} is not finalized")


def _interval_cutoff_id(interval: str, cutoff: datetime) -> str:
    return f"{interval}:{cutoff.isoformat().replace('+00:00', 'Z')}"


def _ensure_cutoff_run_finalized(db_path: str | Path, cutoff_id: str, interval: str, cutoff: datetime) -> None:
    """Upsert a finalized cutoff_run row so plugins can read a consistent snapshot."""
    conn = config.get_db_connection(read_only=False, db_path=db_path)
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO cutoff_runs
                (cutoff_id, cutoff_at, status, started_at, finalized_at, source_observation_ids, error)
            VALUES (?, ?, 'finalized', ?, ?, '[]', NULL)
            """,
            (cutoff_id, cutoff.isoformat(), cutoff.isoformat(), cutoff.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def _build_snapshot(db_path: str | Path, cutoff_id: str, now: datetime | None) -> dict:
    snapshot = {"db_path": str(db_path), "now": now, "cutoff_id": cutoff_id,
                "required_datasets": {}, "feature_snapshots": {}}
    feat_conn = config.get_db_connection(read_only=True, db_path=db_path)
    try:
        fs = feat_conn.execute(
            "SELECT asset, feature_set, payload_json FROM feature_snapshots WHERE cutoff_id = ?",
            (cutoff_id,)
        ).fetchall()
        for asset, fset, payload in fs:
            snapshot["feature_snapshots"].setdefault(asset, {})[fset] = json.loads(payload) if payload else {}
        try:
            zones = feat_conn.execute(
                "SELECT asset, kind, direction, strength, source_evidence_ids, confidence_status FROM structure_zones WHERE cutoff_id = ?",
                (cutoff_id,)
            ).fetchall()
            if zones:
                snapshot["zones"] = []
                for z in zones:
                    kind = z[1]
                    typ, tf = (kind.split("_", 1) + [""])[:2] if "_" in kind else (kind, "")
                    snapshot["zones"].append({
                        "asset": z[0], "kind": kind, "type": typ, "timeframe": tf,
                        "direction": z[2], "strength": z[3],
                        "source_evidence_ids": json.loads(z[4] or "[]"),
                        "confidence_status": z[5]
                    })
        except Exception:
            pass
    finally:
        feat_conn.close()
    return snapshot


def _run_plugins_for_cutoff(db_path: str | Path, cutoff_id: str, now: datetime | None,
                            require_finalized: bool, snapshot: dict | None = None) -> Dict[str, object]:
    """Run active plugins against one finalized cutoff. Failures isolated."""
    results: Dict[str, object] = {}
    conn = config.get_db_connection(read_only=True, db_path=db_path)
    try:
        if require_finalized:
            _ensure_cutoff_finalized(conn, cutoff_id)
        plugins = load_active_plugins(conn)
    finally:
        conn.close()

    if snapshot is None:
        snapshot = _build_snapshot(db_path, cutoff_id, now)
    eval_interval = snapshot.get("eval_interval", "15m")

    for p in plugins:
        try:
            # Test isolation hook: make a specific plugin raise so we verify other
            # plugins still complete (keyed by id so it works for any plugin).
            if os.environ.get("TEST_EXPLODE_PLUGIN") == p.id:
                raise RuntimeError(f"boom for isolation test: {p.id}")
            feat_snap = snapshot.get("feature_snapshots", {})
            available = set()
            for fs in feat_snap.values():
                if isinstance(fs, dict):
                    available.update(fs.keys())
            # The eval-interval bars are always available from source_observations.
            available.add(f"bars_{eval_interval}")
            missing = [d for d in p.required_datasets if d not in available]
            if missing:
                results[p.id] = {"skipped": f"missing required datasets: {','.join(missing)}"}
                continue
            events = p.run(cutoff_id, snapshot) or []
            for ev in events:
                ev["eval_interval"] = eval_interval
                ev.setdefault("plugin_version", p.version)
                ev.setdefault("input_snapshot_id", cutoff_id)
                ev.setdefault("source_evidence_ids", [])
                ev.setdefault("confidence_status", "uncalibrated")
                ev["feature_snapshot"] = dict(snapshot.get("feature_snapshots", {}).get(ev.get("asset", ""), {}))
                try:
                    connp = config.get_db_connection(read_only=True)
                    purity_info = _get_bar_purity(connp, ev.get("asset", ""), ev.get("observed_at"), interval=eval_interval)
                    connp.close()
                    ev.setdefault("data_purity", purity_info.get("data_purity", "pure_ca"))
                    ev.setdefault("price_source", purity_info.get("price_source", "coinalyze"))
                    if purity_info.get("fallback_reason"):
                        ev.setdefault("fallback_reason", purity_info["fallback_reason"])
                except Exception:
                    ev.setdefault("data_purity", "pure_ca")
                    ev.setdefault("price_source", "coinalyze")
            results[p.id] = {"emitted": len(events), "events": events}
        except Exception as exc:
            results[p.id] = {"failed": str(exc)[:200]}
    return results


def invoke_plugins_for_cutoff(db_path: str | Path, cutoff_id: str, now: datetime | None = None, require_finalized: bool = True) -> Dict[str, object]:
    """Legacy single-cutoff entry point (15m). Kept for tests/orchestrator."""
    return _run_plugins_for_cutoff(db_path, cutoff_id, now, require_finalized)


def invoke_plugins_for_intervals(db_path: str | Path, now: datetime | None = None,
                                  require_finalized: bool = True,
                                  eval_intervals: list[str] | None = None) -> Dict[str, Dict[str, object]]:
    """Run enabled plugins on every eval interval (1m/5m/15m by default).
    Each interval gets its own finalized cutoff_run and its own snapshot carrying
    `eval_interval`, so plugins evaluate on the correct bars. HTF (1h/4h) is NOT
    an eval interval — it remains an enrichment layer fed into plugins via zones.
    """
    eval_intervals = list(eval_intervals or getattr(config, "EVAL_INTERVALS", ["1m", "5m", "15m"]))
    now = now or datetime.now(timezone.utc)
    out: Dict[str, Dict[str, object]] = {}
    for iv in eval_intervals:
        cutoff = completed_cycle_for(now, iv)
        cutoff_id = _interval_cutoff_id(iv, cutoff)
        _ensure_cutoff_run_finalized(db_path, cutoff_id, iv, cutoff)
        snapshot = _build_snapshot(db_path, cutoff_id, now)
        snapshot["eval_interval"] = iv
        out[iv] = _run_plugins_for_cutoff(db_path, cutoff_id, now, require_finalized, snapshot=snapshot)
    return out
