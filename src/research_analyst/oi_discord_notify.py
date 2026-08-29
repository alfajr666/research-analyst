"""Post Binance OI rotation digests to Discord after a completed scan."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Callable

import config
from binance_oi_rotation_scanner import SOURCE
from discord_format import (
    format_oi_bar_message,
    format_oi_multi_hour_message,
    multi_hour_boundary,
    parse_timestamp,
)
from discord_transport import DiscordWebhookTransport


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _transport() -> DiscordWebhookTransport | None:
    url = config.DISCORD_OI_WEBHOOK_URL or config.DISCORD_ALPHA_WEBHOOK_URL
    if not url:
        return None
    return DiscordWebhookTransport(url)


def _ensure_delivery_table(connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS discord_oi_deliveries (
            delivery_key VARCHAR PRIMARY KEY,
            kind VARCHAR NOT NULL,
            status VARCHAR NOT NULL,
            attempted_at TIMESTAMP WITH TIME ZONE NOT NULL,
            completed_at TIMESTAMP WITH TIME ZONE,
            response_body VARCHAR,
            error_message VARCHAR
        )
    """)


def _already_sent(connection, delivery_key: str) -> bool:
    row = connection.execute(
        "SELECT status FROM discord_oi_deliveries WHERE delivery_key = ?",
        (delivery_key,),
    ).fetchone()
    return row is not None and row[0] == "sent"


def _record(
    connection,
    delivery_key: str,
    kind: str,
    status: str,
    now: datetime,
    response_body: str | None = None,
    error_message: str | None = None,
) -> None:
    connection.execute("""
        INSERT INTO discord_oi_deliveries
            (delivery_key, kind, status, attempted_at, completed_at, response_body, error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (delivery_key) DO UPDATE SET
            status = excluded.status,
            attempted_at = excluded.attempted_at,
            completed_at = excluded.completed_at,
            response_body = excluded.response_body,
            error_message = excluded.error_message
    """, (
        delivery_key, kind, status, now,
        now if status in {"sent", "failed", "skipped"} else None,
        response_body, error_message,
    ))


def load_multi_hour_rows(connection, window_end: datetime, window_hours: int) -> list[dict]:
    window_end = parse_timestamp(window_end).replace(minute=0, second=0, microsecond=0)
    window_start = window_end - timedelta(hours=window_hours - 1)
    rows = connection.execute("""
        SELECT completed_interval_at, asset, symbol, rank, metrics_json
        FROM binance_oi_rotation_events
        WHERE source = ?
          AND bar_minutes = 60
          AND completed_interval_at >= ?
          AND completed_interval_at <= ?
        ORDER BY completed_interval_at, rank
    """, (SOURCE, window_start, window_end)).fetchall()
    out = []
    for interval, asset, symbol, rank, metrics_json in rows:
        metrics = json.loads(metrics_json) if isinstance(metrics_json, str) else (metrics_json or {})
        out.append({
            "completed_interval_at": interval,
            "asset": asset,
            "symbol": symbol or metrics.get("symbol"),
            "rank": rank if rank is not None else metrics.get("rank"),
            "oi_change_1h_pct": metrics.get("oi_change_1h_pct"),
            "oi_change_1h_usd": metrics.get("oi_change_1h_usd"),
            "open_interest_usd": metrics.get("open_interest_usd"),
            "price_change_1h": metrics.get("price_change_1h"),
            "volume_anomaly": metrics.get("volume_anomaly"),
        })
    return out


def notify_oi_feed(
    feed: dict,
    *,
    transport: DiscordWebhookTransport | None = None,
    db_path: str | None = None,
    now: Callable[[], datetime] = utc_now,
) -> dict[str, str]:
    """Send 1h or short-bar (when candidates) + multi-hour (hourly boundary only).

    Short-bar posts are gated by BINANCE_OI_10M_DISCORD_ENABLED and never trigger multi.
    Empty short bars are always skipped for Discord.
    Idempotent via discord_oi_deliveries using bar-aware keys.
    """
    results: dict[str, str] = {"short": "skipped", "hour": "skipped", "multi": "skipped"}
    active_transport = transport if transport is not None else _transport()
    if active_transport is None:
        return results

    interval = parse_timestamp(feed["completed_interval_at"])
    bm = int(feed.get("bar_minutes", 60))
    is_short = bm != 60
    top_n = config.BINANCE_OI_DISCORD_TOP_N
    window_hours = config.BINANCE_OI_DISCORD_MULTI_HOUR_WINDOW
    connection = config.get_db_connection(db_path=db_path or config.BINANCE_OI_DB_PATH)
    try:
        _ensure_delivery_table(connection)
        moment = now()

        if is_short:
            if not getattr(config, "BINANCE_OI_10M_DISCORD_ENABLED", True):
                results["short"] = "disabled"
            else:
                short_key = f"oi:short:{bm}:{interval.isoformat()}"
                if _already_sent(connection, short_key):
                    results["short"] = "already_sent"
                else:
                    message = format_oi_bar_message(feed, top_n=top_n)
                    if message is None:
                        # always skip empty for short per spec
                        _record(connection, short_key, "short", "skipped", moment, error_message="empty_candidates")
                        results["short"] = "skipped_empty"
                    else:
                        try:
                            response = active_transport.send(message)
                            _record(connection, short_key, "short", "sent", moment, response_body=str(response))
                            results["short"] = "sent"
                        except Exception as error:
                            _record(connection, short_key, "short", "failed", moment, error_message=str(error))
                            results["short"] = "failed"
                            print(f"OI Discord short-bar notify failed: {error}", file=sys.stderr)
            # never multi for short bars
            results["multi"] = "skipped"
        else:
            # hourly path (unchanged behavior)
            hour_key = f"oi:1h:{interval.isoformat()}"
            if _already_sent(connection, hour_key):
                results["hour"] = "already_sent"
            else:
                message = format_oi_bar_message(feed, top_n=top_n)
                if message is None:
                    if config.BINANCE_OI_DISCORD_SKIP_EMPTY:
                        _record(connection, hour_key, "hour", "skipped", moment, error_message="empty_candidates")
                        results["hour"] = "skipped_empty"
                    else:
                        message = (
                            f"**OI ROTATION** · Binance USDM · 1h\n"
                            f"Hour closed: `{interval.strftime('%Y-%m-%d %H:%M UTC')}` · **no qualifying candidates**\n\n"
                            f"_Feed only — not an alpha entry signal_"
                        )
                if message is not None:
                    try:
                        response = active_transport.send(message)
                        _record(connection, hour_key, "hour", "sent", moment, response_body=str(response))
                        results["hour"] = "sent"
                    except Exception as error:
                        _record(connection, hour_key, "hour", "failed", moment, error_message=str(error))
                        results["hour"] = "failed"
                        print(f"OI Discord 1h notify failed: {error}", file=sys.stderr)

            if multi_hour_boundary(interval, window_hours):
                multi_key = f"oi:multi:{interval.isoformat()}:w{window_hours}"
                if _already_sent(connection, multi_key):
                    results["multi"] = "already_sent"
                else:
                    rows = load_multi_hour_rows(connection, interval, window_hours)
                    message = format_oi_multi_hour_message(
                        window_end=interval,
                        window_hours=window_hours,
                        hour_rows=rows,
                        generated_at=moment,
                        top_n=top_n,
                    )
                    try:
                        response = active_transport.send(message)
                        _record(connection, multi_key, "multi", "sent", moment, response_body=str(response))
                        results["multi"] = "sent"
                    except Exception as error:
                        _record(connection, multi_key, "multi", "failed", moment, error_message=str(error))
                        results["multi"] = "failed"
                        print(f"OI Discord multi-hour notify failed: {error}", file=sys.stderr)

        connection.commit()
    finally:
        connection.close()
    return results
