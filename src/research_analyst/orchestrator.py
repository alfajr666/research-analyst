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
from db_maintenance import prune_analyst_db

def _get_or_create_cutoff_run(cutoff_at: datetime, interval: str = "5m") -> str:
    cutoff_id = f"{interval}:{cutoff_at.isoformat().replace('+00:00', 'Z')}"
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


def _source_observation_ids(cutoff_at: datetime, interval: str = "5m") -> list[str]:
    """Return the immutable market observations supporting one cutoff."""
    conn = config.get_db_connection(read_only=True, db_path=config.MARKET_DB_PATH)
    try:
        rows = conn.execute(
            """SELECT observation_id
                 FROM source_observations
                WHERE interval = ? AND source_end = ?
                ORDER BY source_end, observation_id""",
            (interval, cutoff_at),
        ).fetchall()
        return [str(row[0]) for row in rows if row[0]]
    except Exception:
        return []
    finally:
        conn.close()

LAST_EVALUATION_OBSERVABILITY = {}
_RAW_BATCH_LOCK = threading.Lock()
_LAST_ANALYST_MAINTENANCE = 0.0


def _maybe_prune_analyst_db() -> None:
    """Prune analyst snapshots and aged terminal ledgers periodically."""
    global _LAST_ANALYST_MAINTENANCE
    if not getattr(config, "DB_MAINTENANCE_ENABLED", True):
        return
    current = time.monotonic()
    interval = max(60, int(getattr(config, "DB_MAINTENANCE_INTERVAL_SECONDS", 21600)))
    if current - _LAST_ANALYST_MAINTENANCE < interval:
        return
    _LAST_ANALYST_MAINTENANCE = current
    connection = None
    try:
        connection = config.get_db_connection(db_path=config.ANALYST_DB_PATH)
        result = prune_analyst_db(connection)
        print(f"Analyst database maintenance: {result}", flush=True)
    except Exception as exc:
        print(f"Analyst database maintenance failed: {exc}", file=sys.stderr, flush=True)
    finally:
        if connection is not None:
            connection.close()


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

def _summarize_interval_results(results, symbols, feed_metadata):
    attempted_symbols = results.get("_attempted_symbols", len(symbols))
    structural_admission = results.get("_structural_admission")
    strategy_summary = {
        strategy_id: {
            "status": "completed" if isinstance(result, dict) and "emitted" in result else "skipped" if isinstance(result, dict) and "skipped" in result else "failed" if isinstance(result, dict) and "failed" in result else "unknown",
            "emitted": result.get("emitted", 0) if isinstance(result, dict) else 0,
            "attempted_symbols": attempted_symbols if isinstance(result, dict) and "emitted" in result else 0,
            "feed_id": feed_metadata.get("feed_id") if isinstance(result, dict) else None,
            "detail": result.get("skipped") or result.get("failed") if isinstance(result, dict) else None,
        }
        for strategy_id, result in results.items()
        if not strategy_id.startswith("_")
    }
    return {
        "strategies": strategy_summary,
        "symbols_evaluated": attempted_symbols,
        "strategy_evaluations": sum(
            item.get("attempted_symbols", 0) for item in strategy_summary.values()
        ),
        "strategy_scopes": results.get("_strategy_scopes", {}),
        "structural_admission": structural_admission if isinstance(structural_admission, dict) else {},
    }


def _run_pipeline(cutoff_at: datetime | None = None, eval_intervals: list[str] | None = None):
    """Runs the full sequential ingestion, scanning, and alerts pipeline."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n==========================================")
    print(f"PIPELINE RUN: {now_str} UTC")
    print(f"==========================================")
    
    # The gateway owns all market-database writes and schema initialization.
    # run_pipeline initializes the analyst schema once before entering here.
    _maybe_prune_analyst_db()

    print("Market pruning is owned by ws_gateway; analyst retention ran in this process.")

    # Cutoff + plugins (data platform v2 path). Legacy direct evaluators cleaned up post cutover.
    try:
        from strategy_v2_context import completed_cycle_for
        primary_interval = (eval_intervals or ["5m"])[0]
        cutoff_at = cutoff_at or completed_cycle_for(datetime.now(timezone.utc), primary_interval)
        cutoff_id = _get_or_create_cutoff_run(cutoff_at, primary_interval)
        # finalize now that ingestion complete
        conn = config.get_db_connection(db_path=config.ANALYST_DB_PATH)
        try:
            conn.execute(
                """UPDATE cutoff_runs
                      SET status='finalized', finalized_at=?, source_observation_ids=?
                    WHERE cutoff_id=?""",
                (datetime.now(timezone.utc), json.dumps(_source_observation_ids(cutoff_at, primary_interval)), cutoff_id),
            )
            conn.commit()
        finally:
            conn.close()

        from symbol_rotation import subscription_assets
        from regime_session import wait_for_gate_scope
        assets, feed_metadata = subscription_assets(cutoff_at)
        # Regime observations and evaluation both use the completed 5m cadence.
        regime_cutoff_at = completed_cycle_for(cutoff_at, "5m")
        regime_scope = wait_for_gate_scope(
            assets,
            regime_cutoff_at,
            str(feed_metadata.get("feed_id") or "unknown"),
        )
        # Regime enforcement scopes each plugin by family. Feature materialization
        # still covers the full subscription universe so one inactive family does
        # not remove an asset needed by another family.
        print(
            f"Regime-session gate mode={regime_scope['mode']} "
            f"ready={len(regime_scope.get('allowed_assets', assets))} "
            f"blocked={len(regime_scope['blocked_assets'])} "
            f"missing={len(regime_scope['missing_assets'])} "
            f"families={{{', '.join(f'{k}:{len(v)}' for k, v in regime_scope.get('family_assets', {}).items())}}}"
        )

        # Numerical frames and zones are transient outputs of the shared
        # computation context. Persisting an all-universe advisory materialization
        # here duplicated plugin/admission work and created a large recomputable
        # feature-snapshot backlog.
        print("Shared Polars computation deferred to plugin evaluation context.")

        from strategy_plugins import invoke_plugins_for_intervals, ensure_plugin_states
        ensure_plugin_states(config.ANALYST_DB_PATH)
        pres = invoke_plugins_for_intervals(
            config.ANALYST_DB_PATH,
            now=datetime.now(timezone.utc),
            market_db_path=config.MARKET_DB_PATH,
            eval_intervals=eval_intervals,
            cutoff_at=cutoff_at,
            regime_scope=regime_scope,
            effective_universe={
                "assets": list(assets),
                "metadata": dict(feed_metadata),
                "cutoff_at": cutoff_at,
            },
        )
        symbols = assets
        strategies = list(config.STRATEGY_ENABLED_IDS)
        per_interval = {}
        for interval, results in pres.items():
            per_interval[interval] = _summarize_interval_results(results, symbols, feed_metadata)
        actual_evaluations = sum(item["strategy_evaluations"] for item in per_interval.values())
        LAST_EVALUATION_OBSERVABILITY.clear()
        LAST_EVALUATION_OBSERVABILITY.update({
            "strategies_enabled": len(strategies),
            "symbols": symbols,
            "symbols_evaluated": max((item["symbols_evaluated"] for item in per_interval.values()), default=0),
            "strategy_evaluations": actual_evaluations,
            "feed_id": feed_metadata.get("feed_id"),
            "effective_universe_version": feed_metadata.get("effective_universe_version"),
            "fallback_reason": feed_metadata.get("fallback_reason"),
            "regime_session": regime_scope,
            "signals_emitted": sum(v.get("emitted", 0) for interval in per_interval.values() for v in interval["strategies"].values()),
            "by_interval": per_interval,
        })
        print(f"Evaluation observability: {json.dumps(LAST_EVALUATION_OBSERVABILITY, sort_keys=True)}")
        for iv, ivres in pres.items():
            print(f"Plugins [{iv}] for {cutoff_id}: { {k: v.get('emitted', v) for k,v in ivres.items() if not k.startswith('_')} }")

    except Exception as e:
        print(f"Cutoff/plugins error: {e}", file=sys.stderr)
        raise

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
                market_connection.execute(
                    "SELECT MAX(source_end) FROM source_observations WHERE source_end <= ?",
                    (completed_at,),
                ).fetchone()[0]
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
                # Persist compact counters only; per-strategy detail goes to
                # stdout + health.json (watchdog), not the database. Full
                # evaluation payloads made pipeline_runs ~83 KB/row.
                json.dumps({
                    "data_latest_at": latest_data_at.isoformat() if latest_data_at else None,
                    "evaluation": {
                        k: v for k, v in LAST_EVALUATION_OBSERVABILITY.items()
                        if k != "by_interval"
                    },
                }, default=str),
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
    requested_intervals = list(eval_intervals or config.EVAL_INTERVALS)
    if "1m" in requested_intervals:
        raise ValueError("1m evaluation is retired; use 5m")
    config.init_analyst_db()
    run_id = str(uuid4())
    _start_pipeline_run(run_id, datetime.now(timezone.utc))
    try:
        _run_pipeline(cutoff_at=cutoff_at, eval_intervals=eval_intervals)
    except Exception as error:
        _finish_pipeline_run(run_id, "failed", error)
        raise
    _finish_pipeline_run(run_id, "completed")


def publish_intents():
    """Persist admitted events and retry their shared-bus publications."""
    from intent_publisher import IntentPublisher
    print(f"Intent publisher: {IntentPublisher().run_once()}")


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
    def run_once_guarded():
        if not _RAW_BATCH_LOCK.acquire(blocking=False):
            return
        try:
            _publish_raw_signal_batch()
        finally:
            _RAW_BATCH_LOCK.release()

    worker = threading.Thread(target=run_once_guarded, name="raw-signal-batch", daemon=True)
    worker.start()
    return worker

def main():
    parser = argparse.ArgumentParser(description="BTC/ETH Options and Futures Research Ingestion Orchestrator")
    parser.add_argument("--once", action="store_true", help="Run the pipeline once and exit immediately.")
    args = parser.parse_args()
    config.secure_secret_file()
    
    if args.once:
        run_pipeline()
        publish_intents()
    else:
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
                run_pipeline(cutoff_at=cutoff_at, eval_intervals=[payload.get("interval", "5m")])
                try:
                    publish_intents()
                except Exception as error:
                    # Publishing is durable and retried by the next cycle; it
                    # must not turn a successful market evaluation into a retry.
                    print(f"Publisher error after successful pipeline: {error}", file=sys.stderr)
                trigger.rename(trigger.with_suffix(".processed"))
                trigger_raw_signal_batch()
            except KeyboardInterrupt:
                raise
            except Exception as error:
                print(f"Critical error processing {trigger.name}: {error}", file=sys.stderr)
                error_text = str(error)
                if "unknown strategy id" in error_text:
                    # A retired strategy in an already-claimed cutoff cannot be
                    # repaired by retrying the same snapshot indefinitely.
                    trigger.rename(trigger.with_suffix(".failed"))
                else:
                    retry(trigger, error_text, config.EVALUATION_TRIGGER_DIR)
                time.sleep(config.EVALUATION_RECOVERY_SCAN_SECONDS)

if __name__ == "__main__":
    main()
