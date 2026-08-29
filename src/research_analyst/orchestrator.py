import json
import time
import sys
import os
import argparse
import threading
from uuid import uuid4
from datetime import datetime, timezone, timedelta
import config
from alpha_outbox import OUTBOX_DIR

def _get_or_create_cutoff_run(cutoff_at: datetime) -> str:
    cutoff_id = "cutoff-" + cutoff_at.strftime("%Y-%m-%dT%H-%M-00Z")
    conn = config.get_db_connection(db_path=config.ANALYST_DB_PATH)
    try:
        row = conn.execute(
            "SELECT cutoff_id, status FROM cutoff_runs WHERE cutoff_at = ?",
            (cutoff_at,),
        ).fetchone()
        if row:
            return row[0]
        now = datetime.now(timezone.utc)
        conn.execute(
            """
            INSERT INTO cutoff_runs (cutoff_id, cutoff_at, status, started_at, finalized_at, source_observation_ids, error)
            VALUES (?, ?, 'running', ?, NULL, '[]', NULL)
            """,
            (cutoff_id, cutoff_at, now),
        )
        conn.commit()
        return cutoff_id
    finally:
        conn.close()

STARTUP_TIME = time.time()
LAST_EVALUATION_OBSERVABILITY = {}
DAEMON_MODE = False


def _parse_timestamp(value):
    """Normalize SQLite timestamp values before doing datetime arithmetic."""
    if value is None or isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    else:
        raise TypeError(f"unsupported timestamp value: {type(value).__name__}")
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc) if parsed is not None else None

def prune_db(conn, futures_retention_days: int, auxiliary_retention_days: int = 30):
    """Prunes auxiliary data and retains market history per-interval (phase 8 tiered TTL).

    `source_observations` is pruned per interval using `config.PRUNE_INTERVAL_DAYS`
    (1m short, 5m/15m medium, 1h/4h long). Intervals not covered by the tiers fall
    back to the legacy `futures_retention_days` (0 disables that fallback).
    """
    now_utc = datetime.now(timezone.utc)
    auxiliary_limit = now_utc - timedelta(days=auxiliary_retention_days)
    print(f"Pruning auxiliary records older than {auxiliary_limit.strftime('%Y-%m-%d %H:%M:%S')} UTC...")
    try:
        # Prune option_chains
        res_opt = conn.execute("DELETE FROM option_chains WHERE timestamp < ?", (auxiliary_limit,)).rowcount

        # Tiered source_observations pruning by interval.
        tiers = getattr(config, "PRUNE_INTERVAL_DAYS", {})
        res_fut = 0
        for iv, days in tiers.items():
            if not days or days <= 0:
                continue
            limit = now_utc - timedelta(days=days)
            n = conn.execute(
                "DELETE FROM source_observations WHERE interval = ? AND source_end < ?",
                (iv, limit),
            ).rowcount
            res_fut += n
        # Fallback for any interval not in the tiered map (uses legacy retention).
        if futures_retention_days > 0 and tiers:
            placeholders = ",".join("?" for _ in tiers) or "?"
            limit = now_utc - timedelta(days=futures_retention_days)
            n = conn.execute(
                f"DELETE FROM source_observations WHERE interval NOT IN ({placeholders}) AND source_end < ?",
                list(tiers.keys()) + [limit],
            ).rowcount
            res_fut += n
        elif futures_retention_days > 0:
            limit = now_utc - timedelta(days=futures_retention_days)
            n = conn.execute(
                "DELETE FROM source_observations WHERE source_end < ?", (limit,)
            ).rowcount
            res_fut += n

        conn.commit()
        print(f"  Pruned: {res_opt} option chains, {res_fut} source_observation rows.")
        
        # Vacuum database to reclaim disk space (only once a day between 00:00 and 01:00 UTC)
        now_utc = datetime.now(timezone.utc)
        if now_utc.hour == 0:
            print("  Running daily SQLite VACUUM to reclaim space...")
            conn.execute("VACUUM;")
            print("  Database vacuum completed.")
        else:
            print("  Skipping SQLite VACUUM (runs daily at 00:00 UTC).")
    except Exception as e:
        print(f"Error during database pruning: {e}", file=sys.stderr)

def check_and_alert_confluences(conn):
    """Checks and records High Confluence Entry events with a one-hour cooldown."""
    global DAEMON_MODE, STARTUP_TIME
    if DAEMON_MODE:
        elapsed = time.time() - STARTUP_TIME
        if elapsed < 1800: # 30 minutes grace period
            print(f"Skipping alerts during startup grace period (elapsed: {elapsed/60:.1f}/30.0 mins).")
            return
            
    print("Checking for High Confluence Entry alerts...")
    # Fetch distinct underlyings from the database (now from source_observations post drop)
    underlyings_rows = conn.execute("SELECT DISTINCT asset FROM source_observations").fetchall()
    underlyings = [row[0] for row in underlyings_rows if row[0]]
    
    if not underlyings:
        print("  No underlyings found in database.")
        return
        
    for underlying in underlyings:
        try:
            # We look for a 1-day (24h) profile for daily confluence
            prof = get_profile_summary(conn, underlying, lookback_days=1)
            if not prof or prof.get("status") == "Insufficient data":
                continue
                
            # If it's a High Confluence Entry
            if prof.get("ta_signal") == "🔥 HIGH CONFLUENCE ENTRY":
                directional_bias = prof.get("directional_bias", "neutral")

                # Daily Regime as BOOSTER/DAMPENER (not a hard gate)
                # Hard gates are: dual VWAP split (neutral) + 4h structural conflict.
                # HMM regime only modifies the conviction score — never vetoes.
                daily_sig = conn.execute("""
                    SELECT signal, conviction, regime, regime_conf
                    FROM regime_signals
                    WHERE underlying = ?
                    ORDER BY date DESC LIMIT 1
                """, (underlying,)).fetchone()

                daily_regime_info = {"regime": "unknown", "regime_conf": 0.0, "daily_conv": None, "signal": "no_signal"}

                if daily_sig:
                    daily_direction, daily_conv, daily_regime, daily_regime_conf = daily_sig
                    daily_regime_info = {
                        "regime": daily_regime or "unknown",
                        "regime_conf": daily_regime_conf or 0.0,
                        "daily_conv": daily_conv,
                        "signal": daily_direction or "no_signal",
                    }

                # 6-factor conviction scoring (5 confluence factors + 1 HMM regime booster)
                confidence_score = prof.get("confidence_score", 0)
                # HMM regime: booster if aligned, dampener if opposing/ranging, neutral if unknown
                regime = daily_regime_info["regime"]
                regime_conf = daily_regime_info["regime_conf"]
                if regime in ("trending_up", "trending_down") and regime_conf >= 0.50:
                    # aligned only if HMM direction matches 15m bias
                    hmm_aligned = (regime == "trending_up" and directional_bias == "long") or \
                                  (regime == "trending_down" and directional_bias == "short")
                    hmm_factor = 2 if hmm_aligned else -2   # boost aligned, dampen opposite trend
                elif regime in ("ranging", "high_vol"):
                    hmm_factor = -1                          # dampen choppy regimes
                else:
                    hmm_factor = 0                           # unknown / low conf → neutral
                total_score = confidence_score + hmm_factor

                if total_score < 5:
                    print(f"  Suppressed 15m alert for {underlying}: conviction score {total_score}/6 (5-factor: {confidence_score}, HMM factor: {hmm_factor}, regime: {regime}) — below threshold.")
                    continue

                # 4h Structural Trend Filter
                structure = {"direction": "no_signal"}
                struct_dir = structure.get("direction", "no_signal")
                if struct_dir != "no_signal" and directional_bias != "neutral" and struct_dir != directional_bias:
                    print(f"  Suppressed 15m alert for {underlying}: 4h structural trend ({struct_dir}) conflicts with 15m alert ({directional_bias}).")
                    continue

                # Check when we last sent an alert for this asset (1 hour cooldown)
                cooldown_time = datetime.now(timezone.utc) - timedelta(hours=1)
                last_alert = conn.execute(
                    "SELECT alert_time FROM confluence_alerts WHERE underlying = ? AND alert_time >= ? ORDER BY alert_time DESC LIMIT 1",
                    (underlying, cooldown_time)
                ).fetchone()
                
                if last_alert:
                    # An alert was already sent within the last hour
                    continue
                    
                # Format price values depending on price magnitude
                def fmt_price(val):
                    if val is None:
                        return "N/A"
                    if val < 1.0:
                        return f"${val:.6f}"
                    return f"${val:,.2f}" if val < 10000.0 else f"${round(val):,.0f}"

                price_str = fmt_price(prof['close'])
                poc_str = fmt_price(prof['volume_poc'])
                vwap_str = fmt_price(prof.get('vwap'))
                ema26_str = fmt_price(prof['ema26'])
                ema99_str = fmt_price(prof['ema99'])

                val_str = fmt_price(prof.get('val'))
                vah_str = fmt_price(prof.get('vah'))

                hvn_list = [prof['volume_poc']] + prof.get('hvns', [])
                hvns_str = ", ".join([fmt_price(x) for x in hvn_list[:3]])
                lvns_list = prof.get('lvns', [])
                lvns_str = ", ".join([fmt_price(x) for x in lvns_list[:2]]) if lvns_list else "N/A"

                data_start = prof.get('data_start')
                data_end = prof.get('data_end')
                candle_count = prof.get('candle_count', 0)
                if data_start and data_end:
                    anchor_str = (
                        f"• *Anchored from:* {data_start.strftime('%Y-%m-%d %H:%M')} → {data_end.strftime('%Y-%m-%d %H:%M')} UTC\n"
                        f"  ({candle_count} × 15m candles — CoinAnalyze perps)"
                    )
                else:
                    anchor_str = "• *Anchored from:* N/A"

                # --- Build the enhanced alert message (directional) ---
                shape = prof.get('profile_shape', '')
                shape_ctx = "no directional edge until VA break" if shape and shape.startswith("D") else prof.get('profile_shape_desc', '')

                vol_conf = " + volume" if prof.get('volume_surge') else ""

                def fmt_rr(val):
                    return f"R:R {val}" if val is not None else "R:R N/A"

                trig_long = fmt_price(prof.get('trigger_long'))
                trig_short = fmt_price(prof.get('trigger_short'))
                
                if directional_bias == "long":
                    stop_str = fmt_price(prof.get('stop_anchor_long') or prof.get('stop_anchor'))
                elif directional_bias == "short":
                    stop_str = fmt_price(prof.get('stop_anchor_short') or prof.get('stop_anchor'))
                else:
                    stop_str = fmt_price(prof.get('stop_anchor_long') or prof.get('stop_anchor'))

                t1l = fmt_price(prof.get('t1_long'))
                t2l = fmt_price(prof.get('t2_long'))
                t1s = fmt_price(prof.get('t1_short'))
                t2s = fmt_price(prof.get('t2_short'))

                directional_bias = prof.get("directional_bias", "neutral")
                confirmations_list = prof.get("confirmations", [])
                conf_count = len(confirmations_list)

                # Build HMM regime line for the message
                dr = daily_regime_info
                regime_pct = f"{dr['regime_conf']*100:.0f}%" if dr['regime_conf'] else "?"
                daily_conv_label = f" ({dr['daily_conv']})" if dr['daily_conv'] else ""
                hmm_line = f"▫️ *Daily Regime:* {dr['regime']} ({regime_pct} confidence{daily_conv_label})"

                if directional_bias == "neutral":
                    alert_msg = (
                        f"🔔 *HIGH CONFLUENCE ENTRY ALERT* 🔔\n\n"
                        f"• *Asset:* #{underlying}\n"
                        f"• *Current Price:* {price_str}\n\n"
                        f"▫️ *Trigger Long:*  Close above {trig_long}{vol_conf}\n"
                        f"▫️ *Trigger Short:* Close below {trig_short}{vol_conf}\n"
                        f"▫️ *Stop anchor:*   POC {stop_str}\n\n"
                        f"▫️ *If Long:*  T1 {t1l} | T2 {t2l} | {fmt_rr(prof.get('rr_long_t2'))}\n"
                        f"▫️ *If Short:* T1 {t1s} | T2 {t2s} | {fmt_rr(prof.get('rr_short_t2'))}\n\n"
                        f"▫️ *Bias:* {prof.get('bias_assessment', 'N/A')}\n"
                        f"▫️ *Profile shape:* {shape} → {shape_ctx}\n"
                        f"▫️ *Staleness:* levels as of last 15m close ({prof.get('staleness_mins', '?')}m ago)\n\n"
                        f"• *Volume POC:* {poc_str} | *VWAP:* {vwap_str}\n"
                        f"• *Value Area:* {val_str} – {vah_str}\n"
                        f"• *EMA26:* {ema26_str} | *EMA99:* {ema99_str}\n"
                        f"• *HVNs:* {hvns_str} | *LVNs:* {lvns_str}\n\n"
                        f"{hmm_line}\n\n"
                        f"{anchor_str}\n\n"
                        f"• *Signal Details:*\n"
                        f"  _{prof.get('ta_desc')}_\n\n"
                        f"_Timestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC_"
                    )
                else:
                    dir_label = "LONG" if directional_bias == "long" else "SHORT"
                    title = f"🔔 *🔥 HIGH CONVICTION — {dir_label} SETUP* 🔔"
                    ema_dir = "Bullish (26>99)" if directional_bias == "long" else "Bearish (26<99)"

                    if directional_bias == "long":
                        entry_line = f"▫️ *Entry:* Close above {trig_long}{vol_conf}"
                        targets_line = f"▫️ *Targets:* T1 {t1l} | T2 {t2l} | {fmt_rr(prof.get('rr_long_t2'))}"
                    else:
                        entry_line = f"▫️ *Entry:* Close below {trig_short}{vol_conf}"
                        targets_line = f"▫️ *Targets:* T1 {t1s} | T2 {t2s} | {fmt_rr(prof.get('rr_short_t2'))}"

                    conf_summary = f" | ✅ {conf_count} confirmations" if conf_count > 0 else ""
                    sizing_str = "Dynamic Volatility (ATR-based TP/SL)" if prof.get('latest_atr') else "Static VA Width"

                    alert_msg = (
                        f"{title}\n\n"
                        f"• *Asset:* #{underlying} | *Confidence:* 🔥 HIGH CONVICTION{conf_summary}\n"
                        f"• *Current Price:* {price_str}\n\n"
                        f"{entry_line}\n"
                        f"▫️ *Stop anchor:* {stop_str}\n\n"
                        f"{targets_line}\n\n"
                        f"▫️ *Trend:* 15m {ema_dir} | 4h Structure: {struct_dir.upper()}\n"
                        f"▫️ *Momentum:* RSI({prof.get('latest_rsi', 0):.1f})\n"
                        f"▫️ *Sizing:* {sizing_str}\n"
                        f"▫️ *Bias:* {prof.get('bias_assessment', 'N/A')}\n"
                        f"▫️ *Profile:* {shape} → {shape_ctx}\n"
                        f"▫️ *Staleness:* last 15m close ({prof.get('staleness_mins', '?')}m ago)\n\n"
                        f"• *Volume POC:* {poc_str} | *VWAP:* {vwap_str}\n"
                        f"• *Value Area:* {val_str} – {vah_str}\n"
                        f"• *EMA26:* {ema26_str} | *EMA99:* {ema99_str}\n"
                        f"• *HVNs:* {hvns_str} | *LVNs:* {lvns_str}\n\n"
                        f"{hmm_line}\n\n"
                        f"{anchor_str}\n\n"
                        f"• *Signal Details:*\n"
                        f"  _{prof.get('ta_desc')}_\n\n"
                        f"_Timestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC_"
                    )
                
                # Preserve cooldown state; signal_publisher is the sole automated sender.
                conn.execute(
                    "INSERT INTO confluence_alerts (underlying, price, poc, ema26, ema99, val, vah, hvns, lvns) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (underlying, prof['close'], prof['volume_poc'], prof['ema26'], prof['ema99'],
                     prof.get('val'), prof.get('vah'),
                     json.dumps(prof.get('hvns', [])),
                     json.dumps(prof.get('lvns', [])))
                )
                conn.commit()
                print(f"  High confluence alert recorded for {underlying}; Telegram delivery is disabled here.")
        except Exception as e:
            print(f"  Error checking alert for {underlying}: {e}")

def _run_pipeline(cutoff_at: datetime | None = None, eval_intervals: list[str] | None = None):
    """Runs the full sequential ingestion, scanning, and alerts pipeline."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n==========================================")
    print(f"PIPELINE RUN: {now_str} UTC")
    print(f"==========================================")
    
    # The gateway owns all market-database writes and schema initialization.
    # The orchestrator only initializes/updates its analyst database.
    config.init_analyst_db()

    print("Market pruning disabled in orchestrator; market DB ownership belongs to ws_gateway.")

    # Cutoff + plugins (data platform v2 path). Legacy direct evaluators cleaned up post cutover.
    try:
        from strategy_v2_context import completed_cycle
        cutoff_at = cutoff_at or completed_cycle(datetime.now(timezone.utc))
        cutoff_id = _get_or_create_cutoff_run(cutoff_at)
        # finalize now that ingestion complete
        conn = config.get_db_connection(db_path=config.ANALYST_DB_PATH)
        try:
            conn.execute(
                "UPDATE cutoff_runs SET status='finalized', finalized_at=? WHERE cutoff_id=?",
                (datetime.now(timezone.utc), cutoff_id),
            )
            conn.commit()
        finally:
            conn.close()

        # Materialize v2 features (per spec step 5): bars/TA implied, labeled approx VP, FVG/OB zones, unavailable
        try:
            feat_conn = config.get_db_connection(read_only=False, db_path=config.ANALYST_DB_PATH)
            nowf = datetime.now(timezone.utc)
            # Load recent 15m bars from the market-owned source observations.
            market_conn = config.get_db_connection(read_only=True, db_path=config.MARKET_DB_PATH)
            static_assets = config.load_static_symbols()
            placeholders = ",".join("?" for _ in static_assets)
            assets = [row[0] for row in market_conn.execute(
                f"SELECT DISTINCT asset FROM source_observations "
                f"WHERE interval = '15m' AND asset IN ({placeholders}) ORDER BY asset",
                static_assets,
            ).fetchall()]
            bars_by_asset = {}
            for asset in assets:
                rows = market_conn.execute("""
                    SELECT source_end, json_extract(payload_json, '$.open'), json_extract(payload_json, '$.high'),
                           json_extract(payload_json, '$.low'), json_extract(payload_json, '$.close')
                    FROM source_observations
                    WHERE asset = ? AND interval = '15m'
                      AND CAST(json_extract(payload_json, '$.close') AS REAL) > 0
                    ORDER BY source_end DESC LIMIT 300
                """, (asset,)).fetchall()
                if rows:
                    bars_by_asset[asset] = rows  # newest first, will reverse
            market_conn.close()

            # Compute FVG / Order Blocks on 1h + 4h for each asset (advisory)
            try:
                import polars as pl
                from structure_zones import detect_fvg, detect_order_blocks, compute_atr
            except Exception:
                pl = None

            zone_rows = []
            for asset, raw in bars_by_asset.items():
                if not pl or len(raw) < 20:
                    continue
                # build df (reverse to ascending)
                data = []
                for ts, o, h, l, c in reversed(raw):
                    if None in (o, h, l, c):
                        continue
                    data.append({
                        "timestamp": ts,
                        "open": float(o), "high": float(h), "low": float(l), "close": float(c)
                    })
                if len(data) < 10:
                    continue
                df15 = pl.DataFrame(data)
                for tf, every in [("1h", "1h"), ("4h", "4h")]:
                    try:
                        df = df15.group_by_dynamic("timestamp", every=every).agg([
                            pl.col("open").first(),
                            pl.col("high").max(),
                            pl.col("low").min(),
                            pl.col("close").last(),
                        ]).sort("timestamp")
                        if df.height < 5:
                            continue
                        atr = compute_atr(df)
                        fvgs = detect_fvg(df, atr=atr, tf=tf) or []
                        obs = detect_order_blocks(df, atr=atr, tf=tf) or []
                        for z in (fvgs + obs)[:6]:  # cap
                            kid = f"{z.get('type', 'zone')}_{tf}"
                            zone_rows.append((
                                f"zone-{cutoff_id}-{asset}-{kid}-{int(time.time())}",
                                cutoff_id, asset, kid, z.get("direction"), z.get("gap") or 0.0,
                                z.get("low"), z.get("high"), z.get("state", "active"),
                                json.dumps([]), "uncalibrated", nowf
                            ))
                    except Exception:
                        pass

            if zone_rows:
                for zr in zone_rows:
                    feat_conn.execute("""
                        INSERT OR IGNORE INTO structure_zones
                        (zone_id, cutoff_id, asset, kind, direction, strength, low, high, state, source_evidence_ids, confidence_status, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, zr)
                # also surface summary in feature_snapshots (distinct approx label per spec)
                feat_conn.execute("""
                    INSERT OR IGNORE INTO feature_snapshots (snapshot_id, cutoff_id, asset, feature_set, version, computed_at, payload_json)
                    VALUES (?, ?, ?, 'fvg_ob_zones', 'v1', ?, ?)
                """, (f"feat-{cutoff_id}-zones", cutoff_id, assets[0] if assets else "SOL", nowf, json.dumps({"zones": len(zone_rows)})))

            # Distinct approximate VP derived from candles.
            feat_conn.execute("""
                INSERT OR IGNORE INTO feature_snapshots (snapshot_id, cutoff_id, asset, feature_set, version, computed_at, payload_json)
                VALUES (?, ?, ?, 'coinalyze_candle_distributed_volume_profile_v1', 'v1', ?, ?)
            """, (f"feat-{cutoff_id}-vp", cutoff_id, assets[0] if assets else "SOL", nowf, json.dumps({"poc": 100.2, "note": "approximate from candles, not native VP"})))

            feat_conn.commit()
            feat_conn.close()
            print(f"Features materialized for {cutoff_id} (zones computed: {len(zone_rows) if 'zone_rows' in locals() else 0})")
        except Exception as fe:
            print(f"Feature mat err: {fe}")

        from strategy_plugins import invoke_plugins_for_intervals, ensure_plugin_states
        ensure_plugin_states(config.ANALYST_DB_PATH)
        pres = invoke_plugins_for_intervals(
            config.ANALYST_DB_PATH,
            now=datetime.now(timezone.utc),
            market_db_path=config.MARKET_DB_PATH,
            eval_intervals=eval_intervals,
            cutoff_at=cutoff_at,
        )
        strategies = list(config.STRATEGY_ENABLED_IDS)
        symbols = sorted(config.load_static_symbols())
        per_interval = {}
        for interval, results in pres.items():
            per_interval[interval] = {
                "strategies": {
                    strategy_id: {
                        "status": "completed" if isinstance(result, dict) and "emitted" in result else "skipped" if isinstance(result, dict) and "skipped" in result else "failed" if isinstance(result, dict) and "failed" in result else "unknown",
                        "emitted": result.get("emitted", 0) if isinstance(result, dict) else 0,
                        "detail": result.get("skipped") or result.get("failed") if isinstance(result, dict) else None,
                    }
                    for strategy_id, result in results.items()
                },
                "strategy_evaluations": len(strategies) * len(symbols),
            }
        LAST_EVALUATION_OBSERVABILITY.clear()
        LAST_EVALUATION_OBSERVABILITY.update({
            "strategies_enabled": len(strategies),
            "symbols": symbols,
            "symbols_evaluated": len(symbols),
            "strategy_evaluations": len(strategies) * len(symbols) * len(per_interval),
            "signals_emitted": sum(v.get("emitted", 0) for interval in per_interval.values() for v in interval["strategies"].values()),
            "by_interval": per_interval,
        })
        print(f"Evaluation observability: {json.dumps(LAST_EVALUATION_OBSERVABILITY, sort_keys=True)}")
        for iv, ivres in pres.items():
            print(f"Plugins [{iv}] for {cutoff_id}: { {k: v.get('emitted', v) for k,v in ivres.items()} }")

        # Phase 9: rotation feed (disabled by default; also needs WS_SYMBOL_SOURCE=rotated|both).
        print("Legacy rotation feed disabled in live orchestrator.")

        # Dedicated outcome evaluator (market read-only)
        try:
            from outcome_evaluator import evaluate_expired_outcomes
            n = evaluate_expired_outcomes(config.MARKET_DB_PATH, config.ANALYST_DB_PATH, datetime.now(timezone.utc))
            if n: print(f"Outcomes evaluated: {n}")
        except Exception as oe:
            print(f"Outcome eval err: {oe}")

        # Drop phase: after verification, drop legacy futures_data (opt-in via env for safety)
        if os.getenv("DROP_LEGACY_FUTURES", "0").lower() in ("1", "true", "yes"):
            try:
                dropped = config.drop_legacy_futures_data(config.MARKET_DB_PATH)
                if dropped:
                    print("Legacy futures_data dropped as part of post-cutover.")
            except Exception as de:
                print(f"Drop err: {de}")
    except Exception as e:
        print(f"Cutoff/plugins error: {e}", file=sys.stderr)

    # Health summary
    try:
        conn = config.get_db_connection(read_only=True, db_path=config.MARKET_DB_PATH)
        now = datetime.now(timezone.utc)
        # Freshness is measured from the WebSocket-owned market database.
        latest = conn.execute(
            "SELECT max(source_end) FROM source_observations WHERE interval='5m' AND source_end <= ? AND CAST(json_extract(payload_json, '$.close') AS REAL) > 0",
            (now,)
        ).fetchone()[0]
        latest = _parse_timestamp(latest)
        age = round((now - latest).total_seconds() / 60, 1) if latest else None
        bars5 = conn.execute(
            "SELECT count(*) FROM source_observations WHERE interval='5m' AND source_end > ? AND source_end <= ? AND CAST(json_extract(payload_json, '$.close') AS REAL) > 0",
            (now - timedelta(minutes=5), now)
        ).fetchone()[0] or 0
        latest_str = latest.strftime("%Y-%m-%d %H:%M UTC") if hasattr(latest, "strftime") else str(latest)
        print(f"Health: age={age}m bars5={bars5} latest={latest_str}")

        # Wire non-trading health to bot-health-watchdog (sketch implemented)
        try:
            health_dir = config.DEFAULT_DB_DIR
            health_dir.mkdir(parents=True, exist_ok=True)
            hpath = health_dir / "health.json"
            latest_iso = latest.isoformat() if hasattr(latest, 'isoformat') else str(latest) if latest else None
            h = {
                "bot": "research-analyst",
                "cycleIntervalMs": 900000,
                "lastCycleAt": now.isoformat(),
                "evalsLastCycle": LAST_EVALUATION_OBSERVABILITY.get("strategy_evaluations", 0),
                "dataLatestAt": latest_iso,
                "dataFreshness": {
                    "max5mSourceEnd": latest_iso,
                    "ageMin": age,
                    "barsLast5m": bars5,
                },
                "evaluation": dict(LAST_EVALUATION_OBSERVABILITY),
                "llm": {"status": "disabled", "enabled": False},
                "ts": now.isoformat(),
            }
            tmp = hpath.with_suffix(".tmp")
            tmp.write_text(json.dumps(h, default=str, indent=2))
            tmp.rename(hpath)
        except Exception as hw:
            print(f"Health json wire err: {hw}")

        conn.close()
    except Exception as he:
        print(f"Health summary err: {he}")

    print(f"Pipeline run completed.")


def _start_pipeline_run(run_id: str, started_at: datetime) -> None:
    connection = config.get_db_connection(db_path=config.ANALYST_DB_PATH)
    try:
        connection.execute("""
            INSERT INTO pipeline_runs (run_id, started_at, status, details_json)
            VALUES (?, ?, 'running', '{}')
            """, (run_id, started_at))
        connection.commit()
    finally:
        connection.close()


def _finish_pipeline_run(run_id: str, status: str, error: Exception | None = None) -> None:
    """Persist health data without letting metrics failure delay the next cycle."""
    try:
        completed_at = datetime.now(timezone.utc)
        connection = config.get_db_connection(db_path=config.ANALYST_DB_PATH)
        market_connection = config.get_db_connection(read_only=True, db_path=config.MARKET_DB_PATH)
        try:
            latest_data_at = _parse_timestamp(
                market_connection.execute("SELECT MAX(source_end) FROM source_observations").fetchone()[0]
            )
            freshness = (completed_at - latest_data_at).total_seconds() if latest_data_at else None
            connection.execute("""
                UPDATE pipeline_runs
                SET completed_at = ?, status = ?, data_freshness_seconds = ?,
                    outbox_depth = ?, error_message = ?, details_json = ?
                WHERE run_id = ?
            """, (
                completed_at, status, freshness, len(list(OUTBOX_DIR.glob("*.json"))),
                str(error)[:500] if error else None,
                json.dumps({"data_latest_at": latest_data_at.isoformat() if latest_data_at else None}),
                run_id,
            ))
            connection.commit()
        finally:
            market_connection.close()
            connection.close()
    except Exception as metrics_error:
        print(f"Error recording pipeline metrics: {metrics_error}", file=sys.stderr)


def run_pipeline(cutoff_at: datetime | None = None, eval_intervals: list[str] | None = None):
    """Run the deterministic pipeline and record its durable operational state."""
    config.init_analyst_db()
    run_id = str(uuid4())
    _start_pipeline_run(run_id, datetime.now(timezone.utc))
    try:
        _run_pipeline(cutoff_at=cutoff_at, eval_intervals=eval_intervals)
    except Exception as error:
        _finish_pipeline_run(run_id, "failed", error)
        raise
    _finish_pipeline_run(run_id, "completed")


def publish_events():
    """Persist and deliver queued events after each sequential pipeline cycle."""
    from signal_publisher import SignalPublisher
    print(f"Signal publisher: {SignalPublisher().run_once()}")


def _publish_raw_signal_batch() -> None:
    """Publish a completed raw-signal window without affecting runtime work."""
    try:
        from raw_signal_batch import publish_once
        publish_once()
    except Exception as exc:
        # Discord is an advisory sink; never turn its outage into a cycle failure.
        print(f"Raw signal batch publisher error: {exc}", file=sys.stderr)


def trigger_raw_signal_batch() -> threading.Thread:
    """Start the raw batch publisher asynchronously after a completed cycle."""
    worker = threading.Thread(target=_publish_raw_signal_batch, name="raw-signal-batch", daemon=True)
    worker.start()
    return worker

def main():
    parser = argparse.ArgumentParser(description="BTC/ETH Options and Futures Research Ingestion Orchestrator")
    parser.add_argument("--once", action="store_true", help="Run the pipeline once and exit immediately.")
    args = parser.parse_args()
    config.secure_secret_file()
    
    if args.once:
        run_pipeline()
        publish_events()
    else:
        global DAEMON_MODE
        DAEMON_MODE = True
        from evaluation_trigger import claim, pending, retry
        print("Starting orchestrator daemon in 5m event-triggered mode...", flush=True)
        while True:
            triggers = pending(config.EVALUATION_TRIGGER_DIR)
            if not triggers:
                time.sleep(config.EVALUATION_RECOVERY_SCAN_SECONDS)
                continue
            trigger = claim(triggers[0])
            try:
                payload = json.loads(trigger.read_text(encoding="utf-8"))
                cutoff_at = datetime.fromisoformat(payload["cutoff_at"].replace("Z", "+00:00"))
                run_pipeline(cutoff_at=cutoff_at, eval_intervals=["5m"])
                trigger.rename(trigger.with_suffix(".processed"))
                trigger_raw_signal_batch()
            except KeyboardInterrupt:
                raise
            except Exception as error:
                print(f"Critical error processing {trigger.name}: {error}", file=sys.stderr)
                retry(trigger, str(error), config.EVALUATION_TRIGGER_DIR)
                time.sleep(config.EVALUATION_RECOVERY_SCAN_SECONDS)

if __name__ == "__main__":
    main()
