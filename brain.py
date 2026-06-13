"""
Brain Module (v1, hard rules).

Generates a concise one-liner per underlying (BTC, ETH) plus a combined line.
Each line references the most important SHIFT from the previous run when
relevant, falling back to the current top-priority tag when no shift exists.

Internal flow (the "processor"):
  raw brief  -> compute_tags()  -> compare to load_previous_tags()
              -> find_priority_shift()  -> format_underlying_line()
              -> persist_brain()  -> generate_brain_brief()

The user sees ONLY the final one-liner text, appended below the existing
market brief in Telegram. Tables/tags are internal state only.
"""
import json
from datetime import datetime, timezone
import config
from analyze import get_futures_summary, get_options_summary

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Tag priority (highest first) — used to pick the HEADLINE shift when multiple
# change in the same run. Funding and skew are the most actionable for traders.
TAG_PRIORITY = [
    "funding",
    "skew",
    "put_call",
    "iv_regime",
    "ls_ratio",
    "liquidation",
    "oi_trend",
]

# Thresholds — aligned with the existing logic in analyze.py
THRESHOLDS = {
    "funding_high": 0.03,        # > 0.03% per 8h = leveraged long
    "funding_low": -0.01,        # < -0.01% per 8h = leveraged short
    "skew_put": 1.5,             # 25d put-call IV spread > 1.5 vol pts
    "skew_call": -1.5,
    "iv_high": 60.0,             # ATM IV > 60%
    "iv_low": 30.0,              # ATM IV < 30% AND IV rank low
    "iv_rank_high": 50.0,
    "iv_rank_low": 30.0,
    "pcr_put_heavy": 1.2,        # put/call OI ratio
    "pcr_call_heavy": 0.8,
    "ls_long": 1.5,              # long/short account ratio
    "ls_short": 0.67,
    "oi_build": 2.0,             # 24h OI change in %
    "oi_fall": -2.0,
    "liq_dominance": 2.0,        # long liqs > short liqs * 2 = long squeeze
}

# Default phrase per (data_point, tag) — used when no shift from previous run
DEFAULT_PHRASES = {
    "funding": {
        "leveraged_long": "long-leveraged funding",
        "leveraged_short": "short-discount funding",
        "neutral": "balanced funding",
    },
    "skew": {
        "put_skew": "defensive skew (puts bid)",
        "call_skew": "aggressive skew (calls bid)",
        "neutral": "balanced skew",
    },
    "put_call": {
        "put_heavy": "put-heavy positioning",
        "call_heavy": "call-heavy positioning",
        "balanced": "balanced put/call",
    },
    "iv_regime": {
        "high_vol": "high vol regime",
        "low_vol": "low vol regime",
        "neutral": "moderate vol",
    },
    "ls_ratio": {
        "crowded_long": "longs crowded",
        "crowded_short": "shorts crowded",
        "balanced": "balanced long/short",
    },
    "liquidation": {
        "long_squeeze": "longs getting squeezed",
        "short_squeeze": "shorts getting squeezed",
        "quiet": "quiet liquidations",
    },
    "oi_trend": {
        "building": "OI building",
        "falling": "OI unwinding",
        "steady": "OI steady",
    },
}

# Shift phrases — covers the most narratively meaningful transitions.
# Fallback generic phrase is generated for any (from, to) not listed here.
SHIFT_PHRASES = {
    "funding": {
        ("neutral", "leveraged_long"): "funding flipped long-leveraged",
        ("leveraged_long", "neutral"): "funding cooled to neutral",
        ("neutral", "leveraged_short"): "funding flipped short-discount",
        ("leveraged_short", "neutral"): "funding normalized",
        ("leveraged_long", "leveraged_short"): "funding flipped negative",
        ("leveraged_short", "leveraged_long"): "funding flipped positive",
    },
    "skew": {
        ("neutral", "put_skew"): "skew shifted defensive (puts bid)",
        ("put_skew", "neutral"): "skew normalized",
        ("neutral", "call_skew"): "skew shifted aggressive (calls bid)",
        ("call_skew", "neutral"): "skew normalized",
        ("put_skew", "call_skew"): "skew flipped aggressive",
        ("call_skew", "put_skew"): "skew flipped defensive",
    },
    "put_call": {
        ("balanced", "put_heavy"): "put/call tilted defensive",
        ("put_heavy", "balanced"): "put/call normalized",
        ("balanced", "call_heavy"): "put/call tilted aggressive",
        ("call_heavy", "balanced"): "put/call normalized",
    },
    "iv_regime": {
        ("neutral", "high_vol"): "vol regime expanded",
        ("high_vol", "neutral"): "vol regime compressed",
        ("neutral", "low_vol"): "vol regime compressed",
        ("low_vol", "neutral"): "vol regime expanded",
    },
    "ls_ratio": {
        ("balanced", "crowded_long"): "longs crowded in",
        ("crowded_long", "balanced"): "longs de-crowded",
        ("balanced", "crowded_short"): "shorts crowded in",
        ("crowded_short", "balanced"): "shorts de-crowded",
    },
    "liquidation": {
        ("quiet", "long_squeeze"): "longs started squeezing",
        ("long_squeeze", "quiet"): "long squeezes faded",
        ("quiet", "short_squeeze"): "shorts started squeezing",
        ("short_squeeze", "quiet"): "short squeezes faded",
    },
    "oi_trend": {
        ("steady", "building"): "OI started building",
        ("building", "steady"): "OI build plateaued",
        ("steady", "falling"): "OI started unwinding",
        ("falling", "steady"): "OI unwind stalled",
    },
}


# ---------------------------------------------------------------------------
# Tag classification
# ---------------------------------------------------------------------------

def compute_tags(fut: dict, opt: dict) -> dict:
    """Classifies current market state into a dict of fixed tags.

    Returns an empty dict if either summary is missing.
    """
    if not fut or not opt:
        return {}

    fr = fut.get("funding_rate", 0.0) or 0.0
    if fr > THRESHOLDS["funding_high"]:
        funding = "leveraged_long"
    elif fr < THRESHOLDS["funding_low"]:
        funding = "leveraged_short"
    else:
        funding = "neutral"

    skew = opt.get("skew_25d", 0.0) or 0.0
    if skew > THRESHOLDS["skew_put"]:
        skew_tag = "put_skew"
    elif skew < THRESHOLDS["skew_call"]:
        skew_tag = "call_skew"
    else:
        skew_tag = "neutral"

    pcr = opt.get("put_call_ratio", 1.0) or 0.0
    if pcr > THRESHOLDS["pcr_put_heavy"]:
        pcr_tag = "put_heavy"
    elif pcr < THRESHOLDS["pcr_call_heavy"]:
        pcr_tag = "call_heavy"
    else:
        pcr_tag = "balanced"

    iv = opt.get("atm_iv", 0.0) or 0.0
    iv_rank = opt.get("iv_rank", 50.0) or 50.0
    if iv > THRESHOLDS["iv_high"] or iv_rank > THRESHOLDS["iv_rank_high"]:
        iv_tag = "high_vol"
    elif iv < THRESHOLDS["iv_low"] and iv_rank < THRESHOLDS["iv_rank_low"]:
        iv_tag = "low_vol"
    else:
        iv_tag = "neutral"

    ls = fut.get("long_short_ratio", 1.0) or 0.0
    if ls > THRESHOLDS["ls_long"]:
        ls_tag = "crowded_long"
    elif ls < THRESHOLDS["ls_short"]:
        ls_tag = "crowded_short"
    else:
        ls_tag = "balanced"

    oi_change = fut.get("open_interest_change_24h", 0.0) or 0.0
    if oi_change > THRESHOLDS["oi_build"]:
        oi_tag = "building"
    elif oi_change < THRESHOLDS["oi_fall"]:
        oi_tag = "falling"
    else:
        oi_tag = "steady"

    liq_long = fut.get("liq_long_24h", 0.0) or 0.0
    liq_short = fut.get("liq_short_24h", 0.0) or 0.0
    if liq_long > 0 and liq_long > liq_short * THRESHOLDS["liq_dominance"]:
        liq_tag = "long_squeeze"
    elif liq_short > 0 and liq_short > liq_long * THRESHOLDS["liq_dominance"]:
        liq_tag = "short_squeeze"
    else:
        liq_tag = "quiet"

    return {
        "funding": funding,
        "skew": skew_tag,
        "put_call": pcr_tag,
        "iv_regime": iv_tag,
        "ls_ratio": ls_tag,
        "liquidation": liq_tag,
        "oi_trend": oi_tag,
    }


# ---------------------------------------------------------------------------
# Persistence + shift detection
# ---------------------------------------------------------------------------

def load_previous_tags(underlying: str) -> dict:
    """Loads the most recent tags dict for the given underlying.

    Returns {} on cold start (no previous run).
    """
    conn = config.get_db_connection(read_only=True)
    try:
        df = conn.execute(
            """
            SELECT tags_json FROM brain_outputs
            WHERE underlying = ?
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (underlying,),
        ).pl()
        if df.is_empty():
            return {}
        raw = df.to_dicts()[0].get("tags_json")
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    finally:
        conn.close()


def find_priority_shift(curr: dict, prev: dict):
    """Returns (data_point, shift_phrase) for the highest-priority change.

    Returns (None, None) if no previous run or no changes.
    """
    if not prev:
        return None, None
    for dp in TAG_PRIORITY:
        if dp not in curr or dp not in prev:
            continue
        c, p = curr[dp], prev[dp]
        if c == p:
            continue
        phrase = SHIFT_PHRASES.get(dp, {}).get((p, c))
        if phrase is None:
            # Generic fallback so any transition is covered
            phrase = f"{dp} shifted ({p} → {c})"
        return dp, phrase
    return None, None


def persist_brain(underlying: str, tags: dict, summary_line: str) -> None:
    """Writes the brain output for this underlying to brain_outputs."""
    conn = config.get_db_connection(read_only=False)
    try:
        conn.execute(
            """
            INSERT INTO brain_outputs (timestamp, underlying, tags_json, summary_line)
            VALUES (?, ?, ?, ?)
            """,
            (datetime.now(timezone.utc), underlying, json.dumps(tags), summary_line),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# One-liner formatting
# ---------------------------------------------------------------------------

def _default_phrase(tags: dict) -> str:
    """Pick the highest-priority tag's default phrase (used when no shift)."""
    for dp in TAG_PRIORITY:
        if dp in tags:
            return DEFAULT_PHRASES[dp].get(tags[dp], tags[dp])
    return "no clear signal"


def format_underlying_line(underlying: str, fut: dict, opt: dict, prev_tags: dict) -> str:
    """Builds the one-line summary for one underlying."""
    tags = compute_tags(fut, opt)
    _, shift = find_priority_shift(tags, prev_tags)
    phrase = shift if shift else _default_phrase(tags)

    max_pain = opt.get("max_pain", 0.0) or 0.0
    price = fut.get("price", 0.0) or 0.0
    if max_pain > 0:
        level = f"watch ${max_pain:,.0f} max pain"
    else:
        level = f"spot ${price:,.0f}"

    return f"{underlying}: {phrase}; {level}."


def format_combined_line(btc_tags: dict, eth_tags: dict, sol_tags: dict = None) -> str:
    """Builds the combined BTC+ETH+SOL one-liner based on skew bias."""
    if not btc_tags or not eth_tags:
        return "Combined: insufficient data."

    bias_map = {
        "put_skew": "defensive",
        "call_skew": "aggressive",
        "neutral": "balanced",
    }
    btc_bias = bias_map.get(btc_tags.get("skew", "neutral"), "balanced")
    eth_bias = bias_map.get(eth_tags.get("skew", "neutral"), "balanced")

    if sol_tags:
        sol_bias = bias_map.get(sol_tags.get("skew", "neutral"), "balanced")
        if btc_bias == eth_bias == sol_bias:
            if btc_bias == "balanced":
                return "Combined: balanced on all three; rangebound bias."
            return f"Combined: all three {btc_bias}; aligned bias."
        return f"Combined: mixed — BTC {btc_bias}, ETH {eth_bias}, SOL {sol_bias}."

    if btc_bias == eth_bias:
        if btc_bias == "balanced":
            return "Combined: balanced on both; rangebound bias."
        return f"Combined: both {btc_bias}; aligned bias."
    return f"Combined: mixed — BTC {btc_bias}, ETH {eth_bias}."


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_brain_brief() -> str:
    """Generates the brain one-liner for BTC, ETH, SOL and combined.

    Persists current state so the next run can detect shifts.
    Returns a Markdown-formatted string ready to append to the Telegram brief.
    """
    conn = config.get_db_connection(read_only=True)
    try:
        btc_fut = get_futures_summary(conn, "BTC")
        btc_opt = get_options_summary(conn, "BTC")
        eth_fut = get_futures_summary(conn, "ETH")
        eth_opt = get_options_summary(conn, "ETH")
        sol_fut = get_futures_summary(conn, "SOL")
        sol_opt = get_options_summary(conn, "SOL")
    finally:
        conn.close()

    btc_prev = load_previous_tags("BTC")
    eth_prev = load_previous_tags("ETH")
    sol_prev = load_previous_tags("SOL")

    btc_line = format_underlying_line("BTC", btc_fut, btc_opt, btc_prev)
    eth_line = format_underlying_line("ETH", eth_fut, eth_opt, eth_prev)
    sol_line = format_underlying_line("SOL", sol_fut, sol_opt, sol_prev)

    btc_tags = compute_tags(btc_fut, btc_opt)
    eth_tags = compute_tags(eth_fut, eth_opt)
    sol_tags = compute_tags(sol_fut, sol_opt)
    combined_line = format_combined_line(btc_tags, eth_tags, sol_tags)

    # Persist for next-run shift detection
    persist_brain("BTC", btc_tags, btc_line)
    persist_brain("ETH", eth_tags, eth_line)
    persist_brain("SOL", sol_tags, sol_line)
    combined_tags = {
        **btc_tags, 
        **{f"eth_{k}": v for k, v in eth_tags.items()},
        **{f"sol_{k}": v for k, v in sol_tags.items()}
    }
    persist_brain("COMBINED", combined_tags, combined_line)

    return (
        "\n\n— — — — — — — — — —\n"
        "🧠 *Brain*\n"
        f"{btc_line}\n"
        f"{eth_line}\n"
        f"{sol_line}\n"
        f"{combined_line}"
    )


if __name__ == "__main__":
    # Smoke test entry point
    config.init_db()
    print(generate_brain_brief())
