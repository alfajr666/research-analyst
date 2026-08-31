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
from typing import Any, Callable, Dict, List

import config
from alpha_outbox import write_event, dedupe_key
from raw_signal_batch import capture, record_status
from trade_admission import resolve
from strategy_v2_context import atr_last, completed_cycle_for, load_bars_for_interval

# Per spec: re-export from config for modules that imported here before
PRICE_STRUCTURE_STRATEGY_IDS = getattr(config, "PRICE_STRUCTURE_STRATEGY_IDS", set())
MIXED_STRATEGY_IDS = getattr(config, "MIXED_STRATEGY_IDS", set())
ADMISSION_STRATEGY_IDS = {"failed-break-v3", "bb-rsi-meanrev-v1",
                           "williams-fractal-scalp-v1", "ema9-continuation-stochrsi-v1",
                            "dual-zone-follower-v2", "dual-zone-short-follower-v2",
                            "ema20-pullback-h4-trend-v1", "ema-stack-15m-adx-stochrsi-5m-v1"}


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
        if src in (getattr(config, "BYBIT_WS_SOURCE", "bybit_ws"), getattr(config, "BINANCE_WS_SOURCE", "binance_ws")):
            purity = getattr(config, "WS_DATA_PURITY", "pure_ws")
            price_source = src
        else:
            purity = "unknown"
            price_source = src or "unknown"
        return {
            "data_purity": purity,
            "price_source": price_source,
            "fallback_reason": None if purity.startswith("pure_") else "ca_missing_bar",
        }
    except Exception:
        return {"data_purity": "unknown", "price_source": "unknown"}


KNOWN_STRATEGIES = {
    "accumulation-base-v2",
    "impulse-ignition-v2",
    "continuation-breakout-v2",
    "rsi-reclaim-v1",
    "liquidity-sweep-reversal-v1",
    "bb-rsi-meanrev-v1",
    "failed-break-v3",
    "williams-fractal-scalp-v1",
    "ema9-continuation-stochrsi-v1",
    "dual-zone-follower-v2", "dual-zone-short-follower-v2",
    "ema20-pullback-h4-trend-v1", "ema-stack-15m-adx-stochrsi-5m-v1",
}

@dataclass
class StrategyPlugin:
    id: str
    version: str
    required_datasets: tuple[str, ...]
    optional_datasets: tuple[str, ...]
    run: Callable[[str, dict], List[dict]]  # (cutoff_id, snapshot) -> events
    cadence: str | None = None


_REGISTRY: Dict[str, StrategyPlugin] = {}


def register(plugin: StrategyPlugin) -> None:
    _REGISTRY[plugin.id] = plugin


def _load_builtin_plugins():
    from strategies.v2.accumulation_base_v2 import run_plugin as acc_v2_run
    from strategies.v2.impulse_ignition_v2 import run_plugin as ign_v2_run
    from strategies.v2.continuation_breakout_v2 import run_plugin as cont_v2_run
    from strategies.v2.rsi_reclaim_v1 import run_plugin as rsi_reclaim_run
    from strategies.v2.liquidity_sweep_reversal_v1 import run_plugin as lsr_run
    from strategies.compact.bb_rsi_meanrev_v1 import run_plugin as bb_rsi_run
    from strategies.compact.failed_break_v3 import run_plugin as failed_break_run
    from strategies.compact.williams_fractal_scalp_v1 import run_plugin as williams_run
    from strategies.compact.ema9_continuation_stochrsi_v1 import run_plugin as ema9_run
    from strategies.v2.dual_zone_follower_v2 import run_plugin as dual_zone_run
    from strategies.v2.dual_zone_follower_v2 import run_short_plugin as dual_zone_short_run
    from strategies.v2.ema20_pullback_h4_trend_v1 import run_plugin as ema20_run
    from strategies.v2.ema_stack_adx_stochrsi_5m_v1 import run_plugin as stack_run

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
    register(StrategyPlugin("dual-zone-follower-v2", "v2", ("bars_5m",), (), dual_zone_run, "5m"))
    register(StrategyPlugin("dual-zone-short-follower-v2", "v2", ("bars_5m",), (), dual_zone_short_run, "5m"))
    register(StrategyPlugin("ema20-pullback-h4-trend-v1", "v1", ("bars_5m",), (), ema20_run, "5m"))
    register(StrategyPlugin("ema-stack-15m-adx-stochrsi-5m-v1", "v1", ("bars_5m",), (), stack_run, "5m"))


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
    return True


def load_active_plugins(conn=None) -> List[StrategyPlugin]:
    own = conn is None
    if own:
        conn = config.get_db_connection(read_only=True, db_path=config.ANALYST_DB_PATH)
    try:
        return [p for p in load_enabled_plugins() if plugin_effective_active(conn, p.id)]
    finally:
        if own:
            conn.close()


def get_plugin_state(strategy_id: str, db_path: str | Path | None = None) -> dict:
    conn = config.get_db_connection(read_only=True, db_path=db_path or config.ANALYST_DB_PATH)
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
    conn = config.get_db_connection(read_only=False, db_path=db_path or config.ANALYST_DB_PATH)
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
    """Seed plugin_states on first run so the active/inactive flag is explicit."""
    explicit = _explicit_active_set()
    conn = config.get_db_connection(read_only=False, db_path=db_path or config.ANALYST_DB_PATH)
    try:
        now = datetime.now(timezone.utc)
        for sid in KNOWN_STRATEGIES:
            if sid not in config.STRATEGY_ENABLED_IDS:
                state = "inactive"
            elif explicit:
                state = "active" if sid in explicit else "inactive"
            else:
                state = "active"
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
    conn = config.get_db_connection(read_only=False, db_path=db_path or config.ANALYST_DB_PATH)
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
    conn = config.get_db_connection(read_only=False, db_path=db_path or config.ANALYST_DB_PATH)
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
    conn = config.get_db_connection(read_only=True, db_path=db_path or config.ANALYST_DB_PATH)
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
            out.append({"strategy_id": sid, "state": "active",
                        "updated_at": None, "reason": None, "updated_by": None})
    return out


def _ensure_cutoff_finalized(conn, cutoff_id: str) -> None:
    row = conn.execute("SELECT status FROM cutoff_runs WHERE cutoff_id = ?", (cutoff_id,)).fetchone()
    if row is None or row[0] != "finalized":
        raise ValueError(f"cutoff {cutoff_id} is not finalized")


def _interval_cutoff_id(interval: str, cutoff: datetime) -> str:
    return f"{interval}:{cutoff.isoformat().replace('+00:00', 'Z')}"


def _bars_available(market_db_path: str | Path, dataset: str, snapshot: dict) -> bool:
    """Check that the market DB has this bar interval at the snapshot cutoff."""
    interval = dataset.removeprefix("bars_")
    cutoff_id = str(snapshot.get("cutoff_id", ""))
    cutoff_text = cutoff_id.split(":", 1)[1] if ":" in cutoff_id else None
    assets = list((snapshot.get("feature_snapshots") or {}).keys())
    try:
        conn = config.get_db_connection(read_only=True, db_path=market_db_path)
    except Exception:
        return False
    try:
        query = "SELECT 1 FROM source_observations WHERE interval = ?"
        params: list[Any] = [interval]
        if cutoff_text:
            query += " AND source_end <= ?"
            params.append(datetime.fromisoformat(cutoff_text.replace("Z", "+00:00")))
        if assets:
            placeholders = ",".join("?" for _ in assets)
            query += f" AND asset IN ({placeholders})"
            params.extend(assets)
        query += " LIMIT 1"
        try:
            return conn.execute(query, params).fetchone() is not None
        except Exception:
            return False
    finally:
        conn.close()


def _data_freshness_seconds(market_db_path: str | Path, interval: str, cutoff: datetime) -> float | None:
    conn = config.get_db_connection(read_only=True, db_path=market_db_path)
    try:
        try:
            row = conn.execute(
                "SELECT MAX(source_end) FROM source_observations WHERE interval = ? AND source_end <= ?",
                (interval, cutoff),
            ).fetchone()
        except Exception:
            return None
        if not row or row[0] is None:
            return None
        latest = row[0]
        if isinstance(latest, str):
            latest = datetime.fromisoformat(latest.replace("Z", "+00:00"))
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        return max(0.0, (cutoff.astimezone(timezone.utc) - latest.astimezone(timezone.utc)).total_seconds())
    finally:
        conn.close()


def _atr14_4h(market_db_path: str | Path, asset: str, cutoff: datetime) -> float | None:
    conn = config.get_db_connection(read_only=True, db_path=market_db_path)
    try:
        return atr_last(load_bars_for_interval(conn, asset, "4h", cutoff), 14)
    finally:
        conn.close()


def _cutoff_from_id(cutoff_id: str, fallback: datetime | None) -> datetime:
    text = cutoff_id.split(":", 1)[1] if ":" in cutoff_id else cutoff_id[cutoff_id.find("20"):]
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        if fallback is None:
            raise
        return fallback


def _ensure_cutoff_run_finalized(db_path: str | Path, cutoff_id: str, interval: str, cutoff: datetime) -> None:
    """Upsert a finalized cutoff_run row so plugins can read a consistent snapshot."""
    conn = config.get_db_connection(read_only=False, db_path=db_path or config.ANALYST_DB_PATH)
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


def _build_snapshot(db_path: str | Path, cutoff_id: str, now: datetime | None,
                    market_db_path: str | Path | None = None) -> dict:
    snapshot = {"db_path": str(db_path), "market_db_path": str(market_db_path or config.MARKET_DB_PATH), "now": now, "cutoff_id": cutoff_id,
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
                             require_finalized: bool, snapshot: dict | None = None,
                             market_db_path: str | Path | None = None) -> Dict[str, object]:
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
        snapshot = _build_snapshot(db_path, cutoff_id, now, market_db_path)
    eval_interval = snapshot.get("eval_interval", "15m")
    cutoff = _cutoff_from_id(cutoff_id, now)
    freshness = _data_freshness_seconds(snapshot["market_db_path"], eval_interval, cutoff)
    atr_cache = {}

    for p in plugins:
        try:
            if p.cadence is not None and p.cadence != eval_interval:
                results[p.id] = {"skipped": f"cadence {p.cadence}"}
                continue
            # Test isolation hook: make a specific plugin raise so we verify other
            # plugins still complete (keyed by id so it works for any plugin).
            if os.environ.get("TEST_EXPLODE_PLUGIN") == p.id:
                raise RuntimeError(f"boom for isolation test: {p.id}")
            feat_snap = snapshot.get("feature_snapshots", {})
            available = set()
            for fs in feat_snap.values():
                if isinstance(fs, dict):
                    available.update(fs.keys())
            missing = [d for d in p.required_datasets
                       if (d.startswith("bars_") and not _bars_available(
                           snapshot.get("market_db_path") or market_db_path or config.MARKET_DB_PATH,
                           d, snapshot)) or (not d.startswith("bars_") and d not in available)]
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
                    connp = config.get_db_connection(read_only=True, db_path=snapshot["market_db_path"])
                    purity_info = _get_bar_purity(connp, ev.get("asset", ""), ev.get("observed_at"), interval=eval_interval)
                    connp.close()
                    ev.setdefault("data_purity", purity_info.get("data_purity", "unknown"))
                    ev.setdefault("price_source", purity_info.get("price_source", "unknown"))
                    if purity_info.get("fallback_reason"):
                        ev.setdefault("fallback_reason", purity_info["fallback_reason"])
                except Exception:
                    ev.setdefault("data_purity", "unknown")
                    ev.setdefault("price_source", "unknown")
                ev.setdefault("candidate_id", dedupe_key(ev))
                ev["data_freshness_seconds"] = freshness
                asset = ev["asset"]
                if asset not in atr_cache:
                    atr_cache[asset] = _atr14_4h(snapshot["market_db_path"], asset, cutoff)
                ev["atr14_4h"] = ev.get("atr14_4h") or atr_cache[asset]
            results[p.id] = {"emitted": len(events), "events": events}
        except Exception as exc:
            results[p.id] = {"failed": str(exc)[:200]}
    candidates = [ev for sid, result in results.items() if sid in ADMISSION_STRATEGY_IDS
                  and isinstance(result, dict) for ev in result.get("events", [])]
    raw_ids = {ev.get("candidate_id"): capture(ev) for ev in candidates}
    decision = resolve(candidates)
    selected = set(decision["selected_candidate_ids"])
    by_id = {ev["candidate_id"]: ev for ev in candidates}
    for result in decision["results"]:
        raw_id = raw_ids.get(result["candidate_id"])
        if raw_id:
            record_status(raw_id, hard_gate_status=result["hard_gate"], score_status=result["status"],
                          clash_status=result["status"], executor_intent_status="selected" if result["candidate_id"] in selected else "advisory_only",
                          reason="; ".join(result["hard_gate_reasons"]))
    for cid in selected:
        event = by_id[cid]
        event["_admission_result"] = next(r for r in decision["results"] if r["candidate_id"] == cid)
        write_event(event)
    return results


def invoke_plugins_for_cutoff(db_path: str | Path, cutoff_id: str, now: datetime | None = None, require_finalized: bool = True) -> Dict[str, object]:
    """Legacy single-cutoff entry point (15m). Kept for tests/orchestrator."""
    return _run_plugins_for_cutoff(db_path, cutoff_id, now, require_finalized, market_db_path=config.MARKET_DB_PATH)


def invoke_plugins_for_intervals(db_path: str | Path, now: datetime | None = None,
                                  require_finalized: bool = True,
                                  eval_intervals: list[str] | None = None,
                                  market_db_path: str | Path | None = None,
                                  cutoff_at: datetime | None = None) -> Dict[str, Dict[str, object]]:
    """Run enabled plugins on every eval interval (1m/5m/15m by default).
    Each interval gets its own finalized cutoff_run and its own snapshot carrying
    `eval_interval`, so plugins evaluate on the correct bars. HTF (1h/4h) is NOT
    an eval interval — it remains an enrichment layer fed into plugins via zones.
    """
    eval_intervals = list(eval_intervals or getattr(config, "EVAL_INTERVALS", ["1m", "5m", "15m"]))
    now = now or datetime.now(timezone.utc)
    out: Dict[str, Dict[str, object]] = {}
    for iv in eval_intervals:
        cutoff = cutoff_at if cutoff_at is not None and iv == "5m" else completed_cycle_for(now, iv)
        cutoff_id = _interval_cutoff_id(iv, cutoff)
        _ensure_cutoff_run_finalized(db_path, cutoff_id, iv, cutoff)
        snapshot = _build_snapshot(db_path, cutoff_id, now, market_db_path)
        snapshot["eval_interval"] = iv
        out[iv] = _run_plugins_for_cutoff(db_path, cutoff_id, now, require_finalized, snapshot=snapshot, market_db_path=market_db_path)
    return out
