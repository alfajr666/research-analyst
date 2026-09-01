"""Discord markdown formatters for alpha signals and OI rotation digests."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import math


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


_STRATEGY_LABELS = {
    "dual-zone-follower-v2": "Dual-zone follower v2",
    "dual-zone-short-follower-v2": "Dual-zone short follower v2",
    "ema20-pullback-h4-trend-v1": "EMA20 pullback with 4h trend",
    "ema-stack-15m-adx-stochrsi-5m-v1": "EMA stack with ADX/StochRSI",
    "failed-break-v3": "Failed-break reclaim v3",
    "bb-rsi-meanrev-v1": "Bollinger/RSI mean reversion",
    "williams-fractal-scalp-v1": "Williams fractal scalp",
    "ema9-continuation-stochrsi-v1": "EMA9 continuation with StochRSI",
}


def _strategy_label(strategy_id: str) -> str:
    return _STRATEGY_LABELS.get(strategy_id, strategy_id.replace("-", " ").title())


def _phase_label(phase: str) -> str:
    if phase.startswith("channel_"):
        return f"Channel {phase.removeprefix('channel_').upper()}"
    return phase.replace("_", " ").capitalize()


def _setup_label(setup_class: str, phase: str) -> str:
    if setup_class in {"dual_zone_follower", "dual_zone_short_follower"}:
        return "Trend pullback"
    if setup_class == "continuation_breakout":
        return "Breakout continuation"
    if setup_class == "accumulation_base":
        return "Accumulation base"
    if setup_class == "liquidity_reversal":
        return "Liquidity reversal"
    if setup_class in {"impulse_ignition", "squeeze_ignition"}:
        return "Impulse ignition"
    return _phase_label(phase)


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _entry_price(event: dict):
    entry = event.get("entry_price")
    if entry is None:
        entry = (event.get("entry_condition") or {}).get("price")
    return entry if _number(entry) else None


def _entry_label(entry_condition: dict) -> str:
    labels = {
        "limit_at_ema_context": "limit at EMA context",
        "limit_at_ema20_pullback": "limit at EMA20 pullback",
        "breakout_above": "breakout above",
        "breakout_below": "breakout below",
        "market": "market entry",
    }
    entry_type = str(entry_condition.get("type", "entry")).lower()
    return labels.get(entry_type, entry_type.replace("_", " "))


def _status_label(event: dict) -> str | None:
    status = (event.get("_admission_result") or {}).get("status")
    if status == "selected_for_executor":
        return "Admitted · selected"
    if status == "eligible_suppressed_by_same_direction_rank":
        return "Admitted · suppressed by ranking"
    if status == "eligible_suppressed_by_opposite_direction_clash":
        return "Admitted · suppressed by direction clash"
    if status == "hard_gate_failed":
        return "Not admitted"
    return None


def _evidence_lines(event: dict) -> list[str]:
    snapshot = event.get("feature_snapshot") or {}
    setup_class = event.get("setup_class", "")
    direction = str(event.get("direction", "long")).lower()
    entry = _entry_price(event)
    lines: list[str] = []

    if setup_class in {"dual_zone_follower", "dual_zone_short_follower"}:
        ema26 = snapshot.get("ema26")
        ema99 = snapshot.get("ema99")
        if _number(ema26) and _number(ema99):
            relation = "above" if ema26 > ema99 else "below"
            lines.append(f"5m EMA regime: EMA26 {relation} EMA99")
            if _number(entry):
                location = "above" if direction == "long" else "below"
                lines.append(f"5m price: {location} EMA26 and EMA99")
        adx = snapshot.get("adx_1h")
        plus_di = snapshot.get("plus_di_1h")
        minus_di = snapshot.get("minus_di_1h")
        if _number(adx):
            di_text = ""
            if _number(plus_di) and _number(minus_di):
                di_text = "; +DI above -DI" if direction == "long" and plus_di > minus_di else "; -DI above +DI" if direction == "short" and minus_di > plus_di else "; DI direction mixed"
            lines.append(f"1h trend strength: ADX {adx:.1f}{di_text}")
        distance = snapshot.get("entry_distance_pct")
        channel = snapshot.get("channel")
        if _number(distance) and channel:
            anchor = "EMA26" if str(channel).upper() == "A" else "EMA99"
            lines.append(f"Entry zone: {distance:.2f}% from {anchor}")
    elif setup_class == "ema20_pullback_h4_trend":
        ema20 = snapshot.get("ema20_1h")
        ema50 = snapshot.get("ema50_4h")
        ema200 = snapshot.get("ema200_4h")
        if _number(ema20):
            lines.append(f"1h pullback reference: EMA20 {ema20:g}")
        if _number(ema50) and _number(ema200):
            relation = "above" if ema50 > ema200 else "below"
            lines.append(f"4h trend: EMA50 {relation} EMA200")
    elif setup_class == "ema_stack_adx_stochrsi":
        spread = snapshot.get("spread_pct")
        rsi = snapshot.get("rsi")
        if _number(spread):
            lines.append(f"15m/5m EMA stack spread: {spread:.2f}%")
        if _number(rsi):
            lines.append(f"5m RSI: {rsi:.1f}")

    context = _context_parts(snapshot)
    if context and not lines:
        lines.append("Context: " + " · ".join(context))
    return lines


def _risk_lines(event: dict) -> list[str]:
    entry = _entry_price(event)
    stop = event.get("invalidation_price")
    targets = [target for target in event.get("targets") or [] if _number(target)]
    if not (_number(entry) and _number(stop) and entry > 0 and stop > 0 and targets):
        return []
    risk = abs(entry - stop)
    if risk == 0:
        return []
    risk_pct = risk / entry * 100
    rewards = [abs(target - entry) / risk for target in targets]
    reward_text = ", ".join(f"{reward:.2f}R" for reward in rewards)
    return [f"Risk: `{risk:g}` / `{risk_pct:.2f}%`", f"Reward/risk: **{reward_text}**"]


def _alpha_signal_fields(event: dict) -> dict:
    entry_condition = event.get("entry_condition") or {}
    entry = _entry_price(event)
    entry_text = f"`{entry:g}`" if entry is not None else "unavailable"
    entry_text += f" ({_entry_label(entry_condition)})"
    targets = ", ".join(f"`{target:g}`" for target in event.get("targets") or [])
    observed = parse_timestamp(event["observed_at"]).strftime("%Y-%m-%d %H:%M UTC")
    expiry = parse_timestamp(event["valid_until"]).strftime("%Y-%m-%d %H:%M UTC")
    return {
        "direction": event["direction"].upper(),
        "family": _family(event["setup_class"]),
        "strategy": _strategy_label(event["strategy_id"]),
        "strategy_id": event["strategy_id"],
        "setup": _setup_label(event["setup_class"], event["phase"]),
        "phase": _phase_label(event["phase"]),
        "status": _status_label(event),
        "entry": entry_text,
        "invalidation": f"`{event['invalidation_price']:g}`",
        "targets": targets or "unavailable",
        "observed": observed,
        "expiry": expiry,
        "evidence": _evidence_lines(event),
        "risk": _risk_lines(event),
    }


def format_alpha_signal(event: dict, *, markdown: bool = True) -> str:
    """Render the shared alpha message without exposing provisional confidence."""
    fields = _alpha_signal_fields(event)
    if markdown:
        lines = [
            f"**ALPHA SIGNAL · {fields['direction']} · {event['asset']}**",
            f"**Strategy:** {fields['strategy']}",
            f"**Strategy ID:** `{fields['strategy_id']}`",
            f"**Setup:** {fields['setup']} · {fields['phase']}",
        ]
        if fields["status"]:
            lines.append(f"**Status:** {fields['status']}")
        if fields["evidence"]:
            lines.extend(["", "**Why this setup**"])
            lines.extend(f"- {line}" for line in fields["evidence"])
        lines.extend([
            "",
            "**Trade plan**",
            f"- Entry: {fields['entry']}",
            f"- Invalidation: {fields['invalidation']}",
            f"- Target(s): {fields['targets']}",
        ])
        if fields["risk"]:
            lines.extend(["", "**Risk profile**"])
            lines.extend(f"- {line}" for line in fields["risk"])
        lines.extend([
            "",
            "**Validity**",
            f"- Observed: {fields['observed']}",
            f"- Valid until: {fields['expiry']}",
            "",
            "_Execution and fills are not confirmed._",
        ])
        return "\n".join(lines)[:DISCORD_CONTENT_LIMIT]

    lines = [
        "ALPHA SIGNAL",
        f"Strategy: {fields['strategy']}",
        f"Strategy ID: {fields['strategy_id']}",
        f"Asset: {event['asset']}",
        f"Direction: {fields['direction']}",
        f"Setup: {fields['setup']} · {fields['phase']}",
    ]
    if fields["status"]:
        lines.append(f"Status: {fields['status']}")
    if fields["evidence"]:
        lines.append("Why this setup:")
        lines.extend(f"- {line}" for line in fields["evidence"])
    lines.extend([
        "Trade plan:",
        f"- Entry: {fields['entry']}",
        f"- Invalidation: {fields['invalidation']}",
        f"- Target(s): {fields['targets']}",
    ])
    if fields["risk"]:
        lines.append("Risk profile:")
        lines.extend(f"- {line}" for line in fields["risk"])
    lines.extend([
        "Validity:",
        f"- Observed: {fields['observed']}",
        f"- Valid until: {fields['expiry']}",
        "Execution and fills are not confirmed.",
    ])
    return "\n".join(lines)


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
    return format_alpha_signal(event, markdown=True)


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
