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


KNOWN_STRATEGIES = {
    "accumulation-base-v1",
    "impulse-ignition-v1",
    "continuation-breakout-balanced-v1",
    "accumulation-base-v2",
    "impulse-ignition-v2",
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
        if os.environ.get("TEST_EXPLODE_PLUGIN") == "impulse-ignition-v1":
            raise RuntimeError("boom for isolation test")
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

    register(StrategyPlugin("accumulation-base-v1", "v1", ("bars_15m",), ("fvg_1h",), _acc_run))
    register(StrategyPlugin("impulse-ignition-v1", "v1", ("bars_15m",), ("vp",), _ign_run))
    register(StrategyPlugin("continuation-breakout-balanced-v1", "v1", ("bars_15m",), ("acceleration",), _cont_run))
    register(StrategyPlugin("accumulation-base-v2", "v2", ("bars_15m",), ("fvg_1h", "fvg_4h", "vp"), acc_v2_run))
    register(StrategyPlugin("impulse-ignition-v2", "v2", ("bars_15m",), ("fvg_1h", "fvg_4h", "vp"), ign_v2_run))


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


def _ensure_cutoff_finalized(conn, cutoff_id: str) -> None:
    row = conn.execute("SELECT status FROM cutoff_runs WHERE cutoff_id = ?", (cutoff_id,)).fetchone()
    if row is None or row[0] != "finalized":
        raise ValueError(f"cutoff {cutoff_id} is not finalized")


def invoke_plugins_for_cutoff(db_path: str | Path, cutoff_id: str, now: datetime | None = None, require_finalized: bool = True) -> Dict[str, object]:
    """Run enabled plugins against a finalized cutoff. Failures are isolated and reported."""
    results: Dict[str, object] = {}
    plugins = load_enabled_plugins()
    conn = config.get_db_connection(read_only=True, db_path=db_path)
    try:
        if require_finalized:
            _ensure_cutoff_finalized(conn, cutoff_id)
    finally:
        conn.close()

    snapshot = {"db_path": str(db_path), "now": now, "cutoff_id": cutoff_id, "required_datasets": {}, "feature_snapshots": {}}
    # Enrich snapshot immutably from materialized features + zones (per spec)
    feat_conn = config.get_db_connection(read_only=True, db_path=db_path)
    try:
        fs = feat_conn.execute(
            "SELECT asset, feature_set, payload_json FROM feature_snapshots WHERE cutoff_id = ?",
            (cutoff_id,)
        ).fetchall()
        for asset, fset, payload in fs:
            snapshot["feature_snapshots"].setdefault(asset, {})[fset] = json.loads(payload) if payload else {}
        # also load any zones if present (advisory) - tolerant if table absent (some test inits)
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
            pass  # zones optional for this snapshot
    finally:
        feat_conn.close()
    for p in plugins:
        try:
            # check required_datasets (per spec: skip if missing required, report)
            feat_snap = snapshot.get("feature_snapshots", {})
            available = set()
            for fs in feat_snap.values():
                if isinstance(fs, dict):
                    available.update(fs.keys())
            # bars_15m always available from source_observations for finalized cutoff
            missing = [d for d in p.required_datasets if d != "bars_15m" and d not in available]
            if missing:
                results[p.id] = {"skipped": f"missing required datasets: {','.join(missing)}"}
                continue
            events = p.run(cutoff_id, snapshot) or []
            for ev in events:
                # ensure identity includes plugin version + snapshot (per spec)
                ev.setdefault("plugin_version", p.version)
                ev.setdefault("input_snapshot_id", cutoff_id)
                ev.setdefault("source_evidence_ids", [])
                ev.setdefault("confidence_status", "uncalibrated")
                # attach feature snapshot copy (omit unavailable later in publisher)
                ev["feature_snapshot"] = dict(snapshot.get("feature_snapshots", {}).get(ev.get("asset", ""), {}))
            results[p.id] = {"emitted": len(events), "events": events}
        except Exception as exc:
            results[p.id] = {"failed": str(exc)[:200]}
    return results
