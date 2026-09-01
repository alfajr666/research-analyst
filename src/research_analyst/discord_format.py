"""Discord markdown formatters for alpha signals and OI rotation digests."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone


DISCORD_CONTENT_LIMIT = 1900
OI_FOOTER = "_Feed only — not an alpha entry signal_"


def parse_timestamp(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        timestamp = value
    else:
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return timestamp.astimezone(timezone.utc)


def format_usd(value: float | int | None) -> str:
    if value is None:
        return "?"
    number = float(value)
    sign = "-" if number < 0 else ""
    magnitude = abs(number)
    if magnitude >= 1_000_000_000:
        body = f"{magnitude / 1_000_000_000:.2f}B"
    elif magnitude >= 1_000_000:
        body = f"{magnitude / 1_000_000:.2f}M"
    elif magnitude >= 1_000:
        body = f"{magnitude / 1_000:.1f}K"
    else:
        body = f"{magnitude:.0f}"
    if "." in body:
        body = body.rstrip("0").rstrip(".")
    return f"{sign}${body}"


def format_pct(fraction: float | int | None, signed: bool = True) -> str:
    if fraction is None:
        return "?"
    percent = float(fraction) * 100.0
    if signed:
        return f"{percent:+.1f}%"
    return f"{percent:.1f}%"


def _family(setup_class: str) -> str:
    if setup_class in {"dual_zone_follower", "dual_zone_short_follower"}:
        return "Dual-zone trend pullback"
    if setup_class.startswith("continuation"):
        return "Continuation"
    if setup_class == "accumulation_base":
        return "Accumulation base"
    if setup_class == "liquidity_reversal":
        return "Liquidity reversal"
    if setup_class in {"impulse_ignition", "squeeze_ignition"}:
        return "Impulse ignition"
    return "Strategy setup"


def _context_parts(snap: dict) -> list[str]:
    parts = []
    mapping = (
        ("fvg_4h", "4h FVG"),
        ("order_block_4h", "4h OB"),
        ("profile", "profile"),
        ("flow_15m", "15m flow"),
        ("coinalyze_candle_distributed_volume_profile_v1", "approx VP"),
        ("volume_spike_multiple", "vol spike"),
        ("ema_distance_pct", "EMA dist"),
        ("execution_candle", "bar"),
    )
    for key, label in mapping:
        value = snap.get(key)
        if value is None or str(value).lower() == "unavailable":
            continue
        if key == "volume_spike_multiple":
            parts.append(f"{label} {float(value):.2f}×")
        elif key == "ema_distance_pct":
            parts.append(f"{label} {float(value):.2f}%")
        else:
            parts.append(f"{label}:{value}")
    return parts


def format_discord_signal(event: dict) -> str:
    family = _family(event["setup_class"])
    trigger = event["entry_condition"]
    trigger_text = trigger["type"].replace("_", " ")
    if "price" in trigger:
        trigger_text += f" @ `{trigger['price']:g}`"
    targets = ", ".join(f"`{target:g}`" for target in event["targets"])
    observed = parse_timestamp(event["observed_at"]).strftime("%Y-%m-%d %H:%M")
    expiry = parse_timestamp(event["valid_until"]).strftime("%Y-%m-%d %H:%M UTC")
    direction = event["direction"].upper()
    lines = [
        f"**ALPHA · {direction} · {event['asset']}**",
        f"{family} · `{event['strategy_id']}`",
        f"Phase: `{event['phase']}`",
        "",
        f"**Trigger:** {trigger_text}",
        f"**Invalidation:** `{event['invalidation_price']:g}`",
        f"**Targets:** {targets}",
        f"**Window:** {observed} → {expiry}",
    ]
    context = _context_parts(event.get("feature_snapshot") or {})
    if context:
        lines.append("")
        lines.append("Context: " + " · ".join(context))
    return "\n".join(lines)[:DISCORD_CONTENT_LIMIT]


def format_discord_research_note(report: dict) -> str:
    limitations = "; ".join(report.get("limitations", [])[:2])
    note = (
        "\n\n---\n"
        "**Research note** (advisory)\n"
        f"Verdict: **{report['verdict']}**\n"
        f"{report['thesis_summary']}"
    )
    if limitations:
        note += f"\nLimitations: {limitations}"
    return note[:900]


def _candidate_block(candidate: dict, rank_label: str | None = None) -> str:
    rank = rank_label or f"#{candidate.get('rank', '?')}"
    asset = candidate.get("asset", "?")
    symbol = candidate.get("symbol", "")
    header = f"**{rank} {asset}**"
    if symbol:
        header += f" `{symbol}`"
    # Use bar-aware labels when present; fall back to legacy 1h labels (values reflect discovery bar)
    bm = candidate.get("bar_minutes") or candidate.get("bar_minutes", 60)
    delta_label = f"OI Δ {bm}m" if bm != 60 else "OI Δ 1h"
    px_label = f"Price {bm}m" if bm != 60 else "Price 1h"
    vol_label = "Vol anom"
    lines = [
        header,
        (
            f"{delta_label}: **{format_pct(candidate.get('oi_change_1h_pct') or candidate.get('oi_change_bar_pct'))}** "
            f"({format_usd(candidate.get('oi_change_1h_usd') or candidate.get('oi_change_bar_usd'))}) · "
            f"OI {format_usd(candidate.get('open_interest_usd'))}"
        ),
        (
            f"{px_label}: **{format_pct(candidate.get('price_change_1h') or candidate.get('price_change_bar'))}** · "
            f"{vol_label} **{float(candidate.get('volume_anomaly') or 0):.2f}×**"
        ),
    ]
    return "\n".join(lines)


def format_oi_bar_message(feed: dict, top_n: int = 5) -> str | None:
    """Generic formatter for both 1h and short-bar feeds. Uses bar_minutes if present."""
    candidates = list(feed.get("candidates") or [])
    if not candidates:
        return None
    top = candidates[: max(top_n, 0)]
    bm = int(feed.get("bar_minutes", 60))
    kind = "1h" if bm == 60 else f"{bm}m"
    interval = parse_timestamp(feed["completed_interval_at"]).strftime("%Y-%m-%d %H:%M UTC")
    expires = parse_timestamp(feed["expires_at"]).strftime("%H:%M UTC") if feed.get("expires_at") else "?"
    total = len(candidates)
    shown = len(top)
    label = "Hour" if bm == 60 else "Bar"
    header = (
        f"**OI ROTATION** · Binance USDM · {kind}\n"
        f"{label} closed: `{interval}` · top {shown}"
        + (f" of {total}" if total > shown else "")
        + f" · expires `{expires}`"
    )
    body = "\n\n".join(_candidate_block(item) for item in top)
    message = f"{header}\n\n{body}\n\n{OI_FOOTER}"
    return message[:DISCORD_CONTENT_LIMIT]


def format_oi_hour_message(feed: dict, top_n: int = 5) -> str | None:
    """Backward compat wrapper."""
    return format_oi_bar_message(feed, top_n=top_n)


def _hour_label(interval: datetime) -> str:
    return interval.astimezone(timezone.utc).strftime("%H:%M")


def format_oi_multi_hour_message(
    *,
    window_end: datetime,
    window_hours: int,
    hour_rows: list[dict],
    generated_at: datetime | None = None,
    top_n: int = 5,
) -> str:
    """hour_rows: one dict per event with completed_interval_at + candidate fields."""
    window_end = parse_timestamp(window_end)
    window_start = window_end - timedelta(hours=window_hours - 1)
    generated = parse_timestamp(generated_at or datetime.now(timezone.utc))
    by_hour: dict[datetime, list[dict]] = defaultdict(list)
    for row in hour_rows:
        interval = parse_timestamp(row["completed_interval_at"]).replace(minute=0, second=0, microsecond=0)
        by_hour[interval].append(row)

    header = (
        f"**OI ROTATION** · Binance USDM · multi-hour\n"
        f"Window: `{window_start.strftime('%Y-%m-%d %H:%M')}` → "
        f"`{window_end.strftime('%Y-%m-%d %H:%M UTC')}` ({window_hours} completed hours)\n"
        f"Generated: `{generated.strftime('%Y-%m-%d %H:%M UTC')}`"
    )
    if not hour_rows:
        return f"{header}\n**no qualifying candidates**\n\n{OI_FOOTER}"[:DISCORD_CONTENT_LIMIT]

    asset_stats: dict[str, dict] = {}
    for row in hour_rows:
        asset = row["asset"]
        stats = asset_stats.setdefault(
            asset,
            {"hours": set(), "last_rank": None, "last_interval": None, "cum_oi_usd": 0.0, "last_px": None},
        )
        interval = parse_timestamp(row["completed_interval_at"])
        stats["hours"].add(interval.replace(minute=0, second=0, microsecond=0))
        stats["cum_oi_usd"] += float(row.get("oi_change_1h_usd") or 0.0)
        if stats["last_interval"] is None or interval >= stats["last_interval"]:
            stats["last_interval"] = interval
            stats["last_rank"] = row.get("rank")
            stats["last_px"] = row.get("price_change_1h")

    repeats = [
        (asset, stats)
        for asset, stats in asset_stats.items()
        if len(stats["hours"]) >= 2
    ]
    repeats.sort(key=lambda item: (-len(item[1]["hours"]), -item[1]["cum_oi_usd"]))
    sections = [header]

    if repeats:
        lines = ["", f"**Repeat hits** (qualified in ≥2 hours)"]
        for asset, stats in repeats[:top_n]:
            lines.append(
                f"• **{asset}** — {len(stats['hours'])}/{window_hours}h · "
                f"last rank #{stats['last_rank']} · "
                f"cum OI Δ ~{format_usd(stats['cum_oi_usd'])} · "
                f"last px 1h {format_pct(stats['last_px'])}"
            )
        sections.append("\n".join(lines))

    latest = by_hour.get(window_end.replace(minute=0, second=0, microsecond=0), [])
    if latest:
        latest_sorted = sorted(latest, key=lambda item: int(item.get("rank") or 999))[:top_n]
        lines = ["", f"**Latest hour** (`{_hour_label(window_end)} UTC`) — top {len(latest_sorted)}"]
        for item in latest_sorted:
            lines.append(
                f"{item.get('rank', '?')}. {item.get('asset')}  "
                f"{format_pct(item.get('oi_change_1h_pct'))} OI · "
                f"{format_usd(item.get('open_interest_usd'))} · "
                f"px {format_pct(item.get('price_change_1h'))} · "
                f"anom {float(item.get('volume_anomaly') or 0):.2f}×"
            )
        sections.append("\n".join(lines))

    timeline_parts = []
    cursor = window_start
    while cursor <= window_end:
        rows = sorted(by_hour.get(cursor, []), key=lambda item: int(item.get("rank") or 999))
        if rows:
            top = rows[0]
            timeline_parts.append(
                f"`{_hour_label(cursor)}` {top.get('asset')} {format_pct(top.get('oi_change_1h_pct'))}"
            )
        else:
            timeline_parts.append(f"`{_hour_label(cursor)}` —")
        cursor += timedelta(hours=1)
    if timeline_parts:
        sections.append("\n" + "**By hour** (top-1 only, oldest → newest)\n" + " · ".join(timeline_parts))

    sections.append(f"\n{OI_FOOTER}")
    return "\n".join(sections)[:DISCORD_CONTENT_LIMIT]


def multi_hour_boundary(interval: datetime, window_hours: int = 6) -> bool:
    """True on the last completed hour of each multi-hour window (UTC)."""
    hour = parse_timestamp(interval).hour
    return hour % window_hours == (window_hours - 1)
