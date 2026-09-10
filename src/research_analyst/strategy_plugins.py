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
from raw_signal_batch import capture, record_evaluation_coverage, record_status
from entry_policy import annotate_candidate
from trade_admission import canonical_asset, execution_accounts
from trade_quality import resolve
from structural_stop import (
    STRUCTURAL_15M_ADMISSION_CONTRACT_VERSION,
    STRUCTURAL_ADMISSION_CONTRACT_VERSION,
    build_structural_contexts,
)
from strategy_v2_context import (
    completed_cycle_for,
    direct_htf_context, direct_htf_context_active,
    direct_htf_context_evaluation_cutoff, direct_htf_provenance,
    get_shared_computation_context,
    load_bars_for_interval, shared_computation_context,
    shared_computation_context_active, shared_computation_stats,
)
from scope_router import build_strategy_scope
from symbol_rotation import subscription_assets

# Per spec: re-export from config for modules that imported here before
PRICE_STRUCTURE_STRATEGY_IDS = getattr(config, "PRICE_STRUCTURE_STRATEGY_IDS", set())
MIXED_STRATEGY_IDS = getattr(config, "MIXED_STRATEGY_IDS", set())
ADMISSION_STRATEGY_IDS = {"failed-break-v3", "bb-rsi-meanrev-v1",
                           "williams-fractal-scalp-v1", "ema9-continuation-stochrsi-v1",
                             "dual-zone-follower-v3", "dual-zone-short-follower-v3",
                              "ema99-retest-adx-v1",
                            "ema20-pullback-h4-trend-v1", "ema-stack-15m-adx-stochrsi-5m-v1",
                            "gold-trend-ema-bb-stoch-v1", "mtf-exhaustion-reversal-v1",
                              "trend-wall-v1", "ema9-adx-stochrsi-state-v1",
                              "ema99-double-touch-stochrsi-state-v1",
                              "ema7-26-cross-hammer-shooting-star-1h-adx-v1"}


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
    "dual-zone-follower-v3", "dual-zone-short-follower-v3",
    "ema99-retest-adx-v1",
    "ema20-pullback-h4-trend-v1", "ema-stack-15m-adx-stochrsi-5m-v1",
    "gold-trend-ema-bb-stoch-v1", "mtf-exhaustion-reversal-v1", "trend-wall-v1",
    "ema9-adx-stochrsi-state-v1",
    "ema99-double-touch-stochrsi-state-v1",
    "ema7-26-cross-hammer-shooting-star-1h-adx-v1",
}

@dataclass
class StrategyPlugin:
    id: str
    version: str
    required_datasets: tuple[str, ...]
    optional_datasets: tuple[str, ...]
    run: Callable[[str, dict], List[dict]]  # (cutoff_id, snapshot) -> events
    cadence: str | None = None
    market_family: str = "unknown"
    required_intervals: tuple[str, ...] = ()
    feature_requirements: tuple[tuple[str, dict[str, Any]], ...] = ()
    lookback_days: int = 16
    stateful: bool = False


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
    from strategies.v2.dual_zone_follower_v3 import (
        run_plugin as dual_zone_run,
        run_short_plugin as dual_zone_short_run,
    )
    from strategies.v2.ema99_retest_adx_v1 import run_plugin as ema99_retest_run
    from strategies.v2.ema20_pullback_h4_trend_v1 import run_plugin as ema20_run
    from strategies.v2.ema_stack_adx_stochrsi_5m_v1 import run_plugin as stack_run
    from strategies.v2.gold_trend_ema_bb_stoch_v1 import run_plugin as gold_run
    from strategies.v2.mtf_exhaustion_reversal_v1 import run_plugin as exhaustion_run
    from strategies.v2.trend_wall_v1 import run_plugin as wall_run
    from strategies.v2.ema9_adx_stochrsi_state_v1 import run_plugin as ema9_adx_run
    from strategies.v2.ema99_double_touch_stochrsi_state_v1 import run_plugin as ema99_double_touch_run
    from strategies.v2.ema7_26_cross_hammer_shooting_star_v1 import run_plugin as ema7_26_hammer_run

    register(StrategyPlugin("accumulation-base-v2", "v2", ("bars_15m",), ("fvg_1h", "fvg_4h", "vp"), acc_v2_run, "15m", "mean_reversion"))
    register(StrategyPlugin("impulse-ignition-v2", "v2", ("bars_15m",), ("fvg_1h", "fvg_4h", "vp"), ign_v2_run, "15m", "trend"))
    register(StrategyPlugin("continuation-breakout-v2", "v2", ("bars_15m",), ("fvg_1h", "fvg_4h", "vp"), cont_v2_run, "15m", "trend"))
    register(StrategyPlugin("rsi-reclaim-v1", "v1", ("bars_15m",), ("fvg_1h", "fvg_4h", "vp"), rsi_reclaim_run, "15m", "reversal"))
    register(StrategyPlugin("liquidity-sweep-reversal-v1", "v1", ("bars_15m",), ("fvg_1h", "fvg_4h", "vp"), lsr_run, "15m", "reversal"))
    register(StrategyPlugin("bb-rsi-meanrev-v1", "v1", ("bars_5m",), (), bb_rsi_run, "5m", "mean_reversion"))
    # Primary execution bars gate invocation; each plugin loads its own HTF context.
    register(StrategyPlugin("failed-break-v3", "v3", ("bars_5m",), (), failed_break_run, "5m", "reversal"))
    register(StrategyPlugin("williams-fractal-scalp-v1", "v2", ("bars_5m",), (), williams_run, "5m", "trend"))
    register(StrategyPlugin("ema9-continuation-stochrsi-v1", "v2", ("bars_5m",), (), ema9_run, "5m", "trend"))
    register(StrategyPlugin("dual-zone-follower-v3", "v3", ("bars_5m",), (), dual_zone_run, "5m", "trend"))
    register(StrategyPlugin("dual-zone-short-follower-v3", "v3", ("bars_5m",), (), dual_zone_short_run, "5m", "trend"))
    register(StrategyPlugin("ema99-retest-adx-v1", "v1", ("bars_5m",), (), ema99_retest_run, "5m", "trend"))
    register(StrategyPlugin("ema20-pullback-h4-trend-v1", "v1", ("bars_5m",), (), ema20_run, "5m", "trend"))
    register(StrategyPlugin("ema-stack-15m-adx-stochrsi-5m-v1", "v1", ("bars_5m",), (), stack_run, "5m", "trend"))
    register(StrategyPlugin("gold-trend-ema-bb-stoch-v1", "v1", ("bars_5m",), (), gold_run, "5m", "trend"))
    register(StrategyPlugin("mtf-exhaustion-reversal-v1", "v1", ("bars_5m",), (), exhaustion_run, "5m", "reversal"))
    register(StrategyPlugin("trend-wall-v1", "v1", ("bars_5m",), (), wall_run, "5m", "trend"))
    register(StrategyPlugin("ema9-adx-stochrsi-state-v1", "v2", ("bars_5m",), (), ema9_adx_run, "5m", "trend"))
    register(StrategyPlugin("ema99-double-touch-stochrsi-state-v1", "v2", ("bars_5m",), (), ema99_double_touch_run, "5m", "trend"))
    register(StrategyPlugin("ema7-26-cross-hammer-shooting-star-1h-adx-v1", "v1", ("bars_5m",), (), ema7_26_hammer_run, "5m", "reversal"))

    requirements = {
        "failed-break-v3": ("5m", "4h", ("5m", {"stoch": {"stoch": (14, 14, 3, 3)}}), True),
        "bb-rsi-meanrev-v1": ("5m", ("5m", {"rsi": {"rsi": 13}, "atr": {"atr": 14}, "bollinger": {"bb": (30, 2.0)}}), False),
        "williams-fractal-scalp-v1": ("5m", ("5m", {"ema": {"ema_20": 20, "ema_50": 50, "ema_100": 100}}), True),
        "ema9-adx-stochrsi-state-v1": (
            "5m", "1h",
            ("5m", {"ema": {f"ema_{config.EMA9_ADX_EMA_LENGTH}": config.EMA9_ADX_EMA_LENGTH},
                      "rsi": {f"rsi_{config.EMA9_ADX_RSI_LENGTH}": config.EMA9_ADX_RSI_LENGTH},
                      "stoch": {"stoch": (config.EMA9_ADX_RSI_LENGTH, config.EMA9_ADX_STOCH_LENGTH,
                                            config.EMA9_ADX_K_LENGTH, config.EMA9_ADX_D_LENGTH)},
                      "atr": {f"atr_{config.EMA9_ADX_ATR_LENGTH}": config.EMA9_ADX_ATR_LENGTH}}),
            True,
        ),
        "ema99-retest-adx-v1": (
            "5m", "1h",
            ("5m", {"ema": {
                f"ema_{config.EMA99_RETEST_FAST_EMA_LENGTH}": config.EMA99_RETEST_FAST_EMA_LENGTH,
                f"ema_{config.EMA99_RETEST_SLOW_EMA_LENGTH}": config.EMA99_RETEST_SLOW_EMA_LENGTH,
            }, "rsi": {
                f"rsi_{config.EMA99_RETEST_RSI_LENGTH}": config.EMA99_RETEST_RSI_LENGTH,
            }, "atr": {
                f"atr_{config.EMA99_RETEST_ATR_LENGTH}": config.EMA99_RETEST_ATR_LENGTH,
            }}),
            True,
        ),
        "dual-zone-follower-v3": (
            "5m", "15m", "1h",
            ("15m", {"ema": {
                f"ema_{config.DUAL_ZONE_V3_EXIT_EMA_LENGTH}": config.DUAL_ZONE_V3_EXIT_EMA_LENGTH,
                f"ema_{config.DUAL_ZONE_V3_ANCHOR_EMA_LENGTH}": config.DUAL_ZONE_V3_ANCHOR_EMA_LENGTH,
                f"ema_{config.DUAL_ZONE_V3_TREND_EMA_LENGTH}": config.DUAL_ZONE_V3_TREND_EMA_LENGTH,
            }}),
            False,
        ),
        "dual-zone-short-follower-v3": (
            "5m", "15m", "1h",
            ("15m", {"ema": {
                f"ema_{config.DUAL_ZONE_V3_EXIT_EMA_LENGTH}": config.DUAL_ZONE_V3_EXIT_EMA_LENGTH,
                f"ema_{config.DUAL_ZONE_V3_ANCHOR_EMA_LENGTH}": config.DUAL_ZONE_V3_ANCHOR_EMA_LENGTH,
                f"ema_{config.DUAL_ZONE_V3_TREND_EMA_LENGTH}": config.DUAL_ZONE_V3_TREND_EMA_LENGTH,
            }}),
            False,
        ),
        "ema20-pullback-h4-trend-v1": (
            "1h", "4h",
            ("1h", {"ema": {"ema_20": 20}, "atr": {"atr_14": 14}}),
            ("4h", {"ema": {"ema_50": 50, "ema_200": 200}}),
            False,
        ),
        "gold-trend-ema-bb-stoch-v1": (
            "5m",
            ("5m", {"ema": {
                f"ema_{config.GOLD_FAST_EMA}": config.GOLD_FAST_EMA,
                f"ema_{config.GOLD_SLOW_EMA}": config.GOLD_SLOW_EMA,
            }, "stoch": {"stoch": (config.GOLD_RSI_LENGTH, config.GOLD_STOCH_LENGTH,
                                    config.GOLD_K_SMOOTHING, config.GOLD_D_SMOOTHING)},
                     "atr": {f"atr_{config.GOLD_ATR_LENGTH}": config.GOLD_ATR_LENGTH},
                     "bollinger": {"bb": (config.GOLD_BB_LENGTH, config.GOLD_BB_STD)}}),
            False,
        ),
        "mtf-exhaustion-reversal-v1": (
            "5m", "15m", "1h", "4h",
            ("4h", {"rsi": {"rsi4": config.MTF_EXHAUSTION_RSI_LENGTH}}),
            ("1h", {"rsi": {"rsi1": config.MTF_EXHAUSTION_RSI_LENGTH}}),
            ("5m", {"stoch": {"stoch": (14, 14, 3, 3)},
                     "atr": {"atr": config.MTF_EXHAUSTION_ATR_LENGTH}}),
            ("15m", {"vwma": {"vwma": 96}}),
            False,
        ),
        "ema99-double-touch-stochrsi-state-v1": (
            "5m", "1h",
            ("5m", {"ema": {
                         f"ema_{config.EMA99_DOUBLE_TOUCH_EMA_LENGTH}": config.EMA99_DOUBLE_TOUCH_EMA_LENGTH,
                         f"ema_{config.EMA99_DOUBLE_TOUCH_FAST_EMA}": config.EMA99_DOUBLE_TOUCH_FAST_EMA,
                         f"ema_{config.EMA99_DOUBLE_TOUCH_SLOW_EMA}": config.EMA99_DOUBLE_TOUCH_SLOW_EMA,
                     },
                     "rsi": {
                         f"rsi_{config.EMA99_DOUBLE_TOUCH_RSI1_LENGTH}": config.EMA99_DOUBLE_TOUCH_RSI1_LENGTH,
                         f"rsi_{config.EMA99_DOUBLE_TOUCH_RSI5_LENGTH}": config.EMA99_DOUBLE_TOUCH_RSI5_LENGTH,
                     },
                     "stoch": {"stoch": (config.EMA99_DOUBLE_TOUCH_STOCH_RSI_LENGTH,
                                            config.EMA99_DOUBLE_TOUCH_STOCH_LENGTH,
                                            config.EMA99_DOUBLE_TOUCH_K_LENGTH,
                                            config.EMA99_DOUBLE_TOUCH_D_LENGTH)},
                       "atr": {f"atr_{config.EMA99_DOUBLE_TOUCH_ATR_LENGTH}": config.EMA99_DOUBLE_TOUCH_ATR_LENGTH}}),
            True,
        ),
        "ema7-26-cross-hammer-shooting-star-1h-adx-v1": (
            "5m", "1h",
            ("5m", {"ema": {
                f"ema_{config.EMA7_26_CROSS_FAST_EMA}": config.EMA7_26_CROSS_FAST_EMA,
                f"ema_{config.EMA7_26_CROSS_SLOW_EMA}": config.EMA7_26_CROSS_SLOW_EMA,
            }, "rsi": {f"rsi_{config.EMA7_26_CROSS_RSI_LENGTH}": config.EMA7_26_CROSS_RSI_LENGTH},
                     "atr": {f"atr_{config.EMA7_26_CROSS_ATR_LENGTH}": config.EMA7_26_CROSS_ATR_LENGTH}}),
            True,
        ),
    }
    for strategy_id, declaration in requirements.items():
        plugin = _REGISTRY[strategy_id]
        stateful = bool(declaration[-1])
        intervals = tuple(item for item in declaration[:-1] if isinstance(item, str))
        features = tuple(item for item in declaration[:-1] if isinstance(item, tuple))
        plugin.required_intervals = intervals
        plugin.feature_requirements = features
        plugin.stateful = stateful


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
    """Check complete, valid coverage for at least one candidate asset."""
    coverage_cache = snapshot.setdefault("_coverage_cache", {})
    cache_key = (str(market_db_path), dataset, snapshot.get("cutoff_at"),
                 tuple(snapshot.get("subscription_symbols", ())))
    if cache_key in coverage_cache:
        return coverage_cache[cache_key]
    interval = dataset.removeprefix("bars_")
    cutoff = snapshot.get("cutoff_at")
    if isinstance(cutoff, str):
        cutoff = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
    if cutoff is None:
        cutoff_id = str(snapshot.get("cutoff_id", ""))
        cutoff_text = cutoff_id.split(":", 1)[1] if ":" in cutoff_id else None
        if cutoff_text:
            cutoff = datetime.fromisoformat(cutoff_text.replace("Z", "+00:00"))
    if cutoff is None:
        return False
    assets = [asset for _, asset in snapshot.get("subscription_symbols", [])]
    shared_context = get_shared_computation_context()
    try:
        conn = shared_context.market_conn if shared_context is not None else config.get_db_connection(
            read_only=True, db_path=market_db_path
        )
    except Exception:
        coverage_cache[cache_key] = False
        return False
    try:
        try:
            if not assets:
                assets = [row[0] for row in conn.execute(
                    "SELECT DISTINCT asset FROM source_observations WHERE interval = ?",
                    (interval,),
                ).fetchall()]
            from market_coverage import assess_db_coverage
            result = any(
                assess_db_coverage(
                    conn, asset=asset, interval=interval, cutoff=cutoff,
                    expected_bars=1,
                ).status == "covered"
                for asset in sorted(set(assets))
            )
            coverage_cache[cache_key] = result
            return result
        except Exception:
            return False
    finally:
        if shared_context is None:
            conn.close()


def _data_freshness_seconds(market_db_path: str | Path, interval: str, cutoff: datetime,
                            asset: str | None = None) -> float | None:
    shared_context = get_shared_computation_context()
    conn = shared_context.market_conn if shared_context is not None else config.get_db_connection(
        read_only=True, db_path=market_db_path
    )
    try:
        try:
            query = "SELECT MAX(source_end) FROM source_observations WHERE interval = ? AND source_end <= ?"
            params: list[Any] = [interval, cutoff]
            if asset:
                query += " AND asset = ?"
                params.append(asset)
            row = conn.execute(query, params).fetchone()
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
        if shared_context is None:
            conn.close()


def _cutoff_from_id(cutoff_id: str, fallback: datetime | None) -> datetime:
    text = cutoff_id
    if not text[:4].isdigit():
        text = text.split(":", 1)[1] if ":" in text else text[text.find("20"):]
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        if fallback is None:
            raise
        return fallback


def cutoff_for_plugin(cutoff_id: str, snapshot: dict) -> datetime:
    """Return the immutable cutoff supplied to a plugin, never wall-clock time."""
    value = snapshot.get("cutoff_at")
    if value is not None:
        return _cutoff_from_id(str(value), None)
    return _cutoff_from_id(cutoff_id, snapshot.get("now"))


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
    finally:
        feat_conn.close()
    return snapshot


def _run_plugins_for_cutoff(db_path: str | Path, cutoff_id: str, now: datetime | None,
                             require_finalized: bool, snapshot: dict | None = None,
                             market_db_path: str | Path | None = None) -> Dict[str, object]:
    """Run active plugins against one finalized cutoff. Failures isolated."""
    try:
        cutoff = _cutoff_from_id(cutoff_id, now)
    except (TypeError, ValueError):
        # Preserve the original finalized-cutoff error for malformed IDs.
        pass
    else:
        if (not direct_htf_context_active()
                or direct_htf_context_evaluation_cutoff() != cutoff):
            with direct_htf_context(
                getattr(config, "REGIME_DB_PATH", None), cutoff,
                evaluation_cutoff=cutoff,
            ):
                return _run_plugins_for_cutoff(
                    db_path, cutoff_id, now, require_finalized, snapshot=snapshot,
                    market_db_path=market_db_path,
                )
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
    if not shared_computation_context_active():
        with shared_computation_context(market_db_path, cutoff):
            return _run_plugins_for_cutoff(
                db_path, cutoff_id, now, require_finalized,
                snapshot=snapshot, market_db_path=market_db_path,
            )
    eval_interval = snapshot.get("eval_interval", "15m")
    cutoff = _cutoff_from_id(cutoff_id, now)
    snapshot["cutoff_at"] = cutoff
    # Transient Polars frames may be shared by plugins during this invocation,
    # but never enter event snapshots or durable analyst state.
    snapshot.setdefault("_strategy_feature_cache", {})
    snapshot["_coverage_cache"] = {}
    supplied_universe = snapshot.get("effective_universe")
    if isinstance(supplied_universe, dict):
        attempted_symbols = [
            str(asset).upper() for asset in supplied_universe.get("assets", [])
            if str(asset).strip()
        ]
        feed_metadata = dict(supplied_universe.get("metadata") or {})
    else:
        attempted_symbols, feed_metadata = subscription_assets(cutoff)
    regime_scope = snapshot.get("regime_scope")
    if not isinstance(regime_scope, dict):
        # Direct plugin callers without an orchestrator scope are isolated
        # research runs, not an enforce-mode production evaluation.
        regime_scope = {"mode": "off"}
    snapshot["attempted_symbols"] = len(attempted_symbols)
    snapshot["subscription_feed_id"] = feed_metadata.get("feed_id")
    snapshot["effective_universe_version"] = feed_metadata.get(
        "effective_universe_version", feed_metadata.get("feed_id", "unknown")
    )
    snapshot["subscription_symbols"] = list(zip(
        config.expand_perp_symbols(attempted_symbols, "bybit"), attempted_symbols
    ))
    results["_attempted_symbols"] = len(attempted_symbols)
    results["_strategy_scopes"] = {}
    freshness_cache: dict[str, float | None] = {}
    candidates: list[dict] = []
    raw_ids: dict[str, str | None] = {}

    for p in plugins:
        try:
            if p.cadence is not None and p.cadence != eval_interval:
                results[p.id] = {"skipped": f"cadence {p.cadence}"}
                continue
            strategy_scope = build_strategy_scope(
                attempted_symbols,
                plugin_id=p.id,
                market_family=p.market_family,
                cutoff=cutoff,
                feed_metadata=feed_metadata,
                regime_scope=regime_scope,
            )
            results["_strategy_scopes"][p.id] = strategy_scope
            plugin_symbols = list(strategy_scope["allowed_assets"])
            if regime_scope.get("mode") == "enforce" and not plugin_symbols:
                reason = (
                    "unknown strategy family"
                    if any(item["reason"] == "unknown_strategy_family"
                           for item in strategy_scope["excluded_assets"])
                    else "regime session: family has no active assets"
                )
                results[p.id] = {"skipped": reason}
                continue
            computation_context = get_shared_computation_context()
            if computation_context is not None:
                for asset in plugin_symbols:
                    for interval in p.required_intervals:
                        computation_context.load_bars(None, asset, interval, cutoff, p.lookback_days)
                    for interval, feature_spec in p.feature_requirements:
                        computation_context.features(
                            asset, interval, feature_spec,
                            cutoff=cutoff, lookback_days=p.lookback_days,
                        )
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
            plugin_snapshot = dict(snapshot)
            plugin_snapshot["attempted_symbols"] = len(plugin_symbols)
            plugin_snapshot["strategy_scope"] = strategy_scope
            plugin_snapshot["subscription_symbols"] = list(zip(
                config.expand_perp_symbols(plugin_symbols, "bybit"), plugin_symbols
            ))
            events = p.run(cutoff_id, plugin_snapshot) or []
            emitted_counts: dict[str, int] = {}
            for ev in events:
                ev["eval_interval"] = eval_interval
                ev.setdefault("plugin_version", p.version)
                ev.setdefault("input_snapshot_id", cutoff_id)
                ev.setdefault("source_evidence_ids", [])
                ev.setdefault("confidence_status", "uncalibrated")
                ev.setdefault("market_family", p.market_family)
                # Plugin evidence is part of the replay contract. Generic
                # materialized features must never replace it.
                ev.setdefault("feature_snapshot", {})
                ev["feature_snapshot"] = dict(ev["feature_snapshot"])
                if direct_htf_context_active():
                    for interval in ("1h", "4h"):
                        load_bars_for_interval(None, ev.get("asset", ""), interval, cutoff)
                htf_provenance = direct_htf_provenance(ev.get("asset", ""))
                if htf_provenance:
                    ev["engine_htf_provenance"] = htf_provenance
                try:
                    shared_context = get_shared_computation_context()
                    connp = shared_context.market_conn if shared_context is not None else config.get_db_connection(
                        read_only=True, db_path=snapshot["market_db_path"]
                    )
                    try:
                        purity_info = _get_bar_purity(
                            connp, ev.get("asset", ""), ev.get("observed_at"), interval=eval_interval
                        )
                    finally:
                        if shared_context is None:
                            connp.close()
                    ev.setdefault("data_purity", purity_info.get("data_purity", "unknown"))
                    ev.setdefault("price_source", purity_info.get("price_source", "unknown"))
                    if purity_info.get("fallback_reason"):
                        ev.setdefault("fallback_reason", purity_info["fallback_reason"])
                except Exception:
                    ev.setdefault("data_purity", "unknown")
                    ev.setdefault("price_source", "unknown")
                ev.setdefault("candidate_id", dedupe_key(ev))
                ev.update(annotate_candidate(ev))
                asset = ev["asset"]
                if asset not in freshness_cache:
                    freshness_cache[asset] = _data_freshness_seconds(
                        snapshot["market_db_path"], eval_interval, cutoff, asset=canonical_asset(asset),
                    )
                ev["data_freshness_seconds"] = freshness_cache[asset]
                if p.id in ADMISSION_STRATEGY_IDS:
                    target_accounts = execution_accounts(ev, attempted_symbols)
                    if p.id in getattr(config, "COMPACT_STRATEGY_IDS", ()):
                        for account in target_accounts:
                            account_event = dict(ev)
                            account_event["_execution_account"] = account
                            account_event["candidate_id"] = (
                                f"{ev.get('candidate_id')}|account:{account}"
                            )
                            candidates.append(account_event)
                            raw_ids[account_event.get("candidate_id")] = capture(account_event)
                    else:
                        candidates.append(ev)
                        raw_ids[ev.get("candidate_id")] = capture(ev)
                    canonical = canonical_asset(asset)
                    emitted_counts[canonical] = emitted_counts.get(canonical, 0) + 1
            if p.id in ADMISSION_STRATEGY_IDS:
                record_evaluation_coverage(
                    p.id, cutoff, plugin_symbols, emitted_counts, db_path=db_path,
                )
            results[p.id] = {"emitted": len(events), "events": events}
        except Exception as exc:
            results[p.id] = {"failed": str(exc)[:200]}
    structural_contexts = build_structural_contexts(
        candidates,
        cutoff,
        regime_db_path=config.REGIME_DB_PATH,
        market_db_path=snapshot.get("market_db_path") or market_db_path or config.MARKET_DB_PATH,
    )
    market_bars_by_asset = {}
    market_lookback_days = max(
        2,
        int(getattr(config, "TRADE_QUALITY_RVOL_LOOKBACK_BARS", 96) / 288) + 2,
        int(getattr(config, "TRADE_QUALITY_FUNDING_LOOKBACK_BARS", 288) / 288) + 2,
    )
    for asset in {canonical_asset(candidate.get("asset")) for candidate in candidates}:
        try:
            market_bars_by_asset[asset] = load_bars_for_interval(
                None, asset, "5m", cutoff, lookback_days=market_lookback_days,
            )
        except Exception as exc:
            print(f"trade-quality market context unavailable for {asset}: {exc}")
    decision = resolve(
        candidates,
        structural_contexts=structural_contexts,
        market_bars_by_asset=market_bars_by_asset,
        regime_scope=regime_scope,
        now=now,
        effective_universe=attempted_symbols,
        effective_universe_version=feed_metadata.get("effective_universe_version"),
    )
    structural_15m_enabled = bool(getattr(config, "STRUCTURAL_15M_ZONES_ENABLED", False))
    timeframe_counts = {timeframe: 0 for timeframe in ("4h", "1h", "15m", "none")}
    fifteen_rejections: dict[str, int] = {}
    for admission in decision["results"]:
        timeframe = admission.get("selected_zone_timeframe")
        timeframe_counts[timeframe if timeframe in timeframe_counts else "none"] += 1
        for reason in admission.get("structural_stop_reasons", []):
            if "15m" in reason:
                fifteen_rejections[reason] = fifteen_rejections.get(reason, 0) + 1
    loaded_15m = sum(
        1 for context in structural_contexts.values()
        if "15m" in (context.get("coverage_status") or {})
    )
    ready_15m = sum(
        1 for context in structural_contexts.values()
        if (context.get("coverage_status") or {}).get("15m") == "covered"
    )
    results["_structural_admission"] = {
        "structural_15m_zones_enabled": structural_15m_enabled,
        "structural_contract_version": (
            STRUCTURAL_15M_ADMISSION_CONTRACT_VERSION
            if structural_15m_enabled else STRUCTURAL_ADMISSION_CONTRACT_VERSION
        ),
        "candidate_assets": len(structural_contexts),
        "15m_loaded_assets": loaded_15m,
        "15m_ready_assets": ready_15m,
        "15m_unavailable_assets": loaded_15m - ready_15m,
        "selected_timeframe_counts": timeframe_counts,
        "15m_rejection_counts": fifteen_rejections,
        "evaluation_cutoff": cutoff.isoformat(),
        "effective_feed_id": feed_metadata.get("feed_id"),
    }
    selected = set(decision["selected_candidate_ids"])
    by_id = {ev["candidate_id"]: ev for ev in candidates}
    for result in decision["results"]:
        raw_id = raw_ids.get(result["candidate_id"])
        if raw_id:
            policy_failed = result.get("symbol_account_gate") == "fail"
            hard_failed = result.get("hard_gate") != "pass"
            selected_candidate = result["candidate_id"] in selected
            conflict = result.get("status") in {
                "eligible_suppressed_by_opposite_direction_clash", "advisory_only",
            }
            record_status(
                raw_id,
                hard_gate_status=result["hard_gate"],
                score_status="pending" if policy_failed or hard_failed else "scored",
                clash_status="pending" if policy_failed or hard_failed else "conflict" if conflict else "selected" if selected_candidate else "suppressed",
                executor_intent_status="not_eligible" if policy_failed or hard_failed or selected_candidate else "not_selected",
                reason="; ".join(result["hard_gate_reasons"]) or result.get("status"),
                score=result.get("score"),
                score_components=result.get("components"),
                score_policy_version=result.get("score_policy_version"),
                conflict_group_key=(
                    f"{canonical_asset(by_id[result['candidate_id']].get('asset'))}+"
                    f"{by_id[result['candidate_id']].get('cutoff_at') or cutoff.isoformat()}"
                ),
            )
    for cid in selected:
        event = by_id[cid]
        admission_result = next(r for r in decision["results"] if r["candidate_id"] == cid)
        event["_admission_result"] = admission_result
        event["_score_result"] = admission_result
        event["_regime_mode"] = regime_scope.get("mode", "off")
        asset_key = canonical_asset(event.get("asset"))
        event["_regime_decision"] = (regime_scope.get("decisions") or {}).get(asset_key)
        event["structural_context"] = structural_contexts.get(canonical_asset(event.get("asset")))
        context = event["structural_context"] or {}
        selected_zone = next(
            (zone for zone in context.get("zones", [])
             if zone.get("zone_id") == admission_result.get("selected_zone_id")),
            None,
        )
        if selected_zone is not None:
            event["structural_reference"] = dict(selected_zone)
        write_event(event)
    results["_computation_stats"] = shared_computation_stats()
    return results


def invoke_plugins_for_cutoff(db_path: str | Path, cutoff_id: str, now: datetime | None = None, require_finalized: bool = True) -> Dict[str, object]:
    """Legacy single-cutoff entry point (15m). Kept for tests/orchestrator."""
    return _run_plugins_for_cutoff(db_path, cutoff_id, now, require_finalized, market_db_path=config.MARKET_DB_PATH)


def invoke_plugins_for_intervals(db_path: str | Path, now: datetime | None = None,
                                  require_finalized: bool = True,
                                  eval_intervals: list[str] | None = None,
                                  market_db_path: str | Path | None = None,
                                  cutoff_at: datetime | None = None,
                                  regime_scope: dict | None = None,
                                  effective_universe: dict | None = None) -> Dict[str, Dict[str, object]]:
    """Run enabled plugins on every configured eval interval (5m by default).
    Each interval gets its own finalized cutoff_run and its own snapshot carrying
    `eval_interval`, so plugins evaluate on the correct bars. HTF (1h/4h) is NOT
    an eval interval — it remains an enrichment layer fed into plugins via zones.
    """
    eval_intervals = list(eval_intervals or getattr(config, "EVAL_INTERVALS", ["5m"]))
    if "1m" in eval_intervals:
        raise ValueError("1m evaluation is retired; use 5m")
    now = now or datetime.now(timezone.utc)
    out: Dict[str, Dict[str, object]] = {}
    for iv in eval_intervals:
        cutoff = cutoff_at if cutoff_at is not None and (iv == "5m" or len(eval_intervals) == 1) else completed_cycle_for(now, iv)
        cutoff_id = _interval_cutoff_id(iv, cutoff)
        _ensure_cutoff_run_finalized(db_path, cutoff_id, iv, cutoff)
        snapshot = _build_snapshot(db_path, cutoff_id, now, market_db_path)
        snapshot["eval_interval"] = iv
        if effective_universe is not None:
            interval_universe = effective_universe
            supplied_cutoff = effective_universe.get("cutoff_at")
            if supplied_cutoff is not None:
                if isinstance(supplied_cutoff, datetime):
                    supplied_cutoff = (
                        supplied_cutoff.replace(tzinfo=timezone.utc)
                        if supplied_cutoff.tzinfo is None
                        else supplied_cutoff.astimezone(timezone.utc)
                    )
                else:
                    supplied_cutoff = _cutoff_from_id(str(supplied_cutoff), None)
                if supplied_cutoff != cutoff:
                    assets, metadata = subscription_assets(cutoff)
                    interval_universe = {
                        "assets": assets,
                        "metadata": metadata,
                        "cutoff_at": cutoff,
                    }
            snapshot["effective_universe"] = interval_universe
        if regime_scope is not None:
            snapshot["regime_scope"] = regime_scope
        out[iv] = _run_plugins_for_cutoff(db_path, cutoff_id, now, require_finalized, snapshot=snapshot, market_db_path=market_db_path)
    return out
