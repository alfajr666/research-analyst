import duckdb
import polars as pl
from datetime import datetime, timezone, timedelta
import config

def get_futures_summary(conn, underlying: str) -> dict:
    """Computes futures metrics from the futures_data table."""
    # We want the latest record and a record from 24h ago
    now = datetime.now(timezone.utc)
    one_day_ago = now - timedelta(days=1)
    
    # 1. Fetch latest record
    latest_df = conn.execute(f"""
        SELECT timestamp, open_interest, funding_rate, predicted_funding,
               long_short_ratio, close, open, high, low, volume
        FROM futures_data
        WHERE underlying = '{underlying}'
        ORDER BY timestamp DESC
        LIMIT 1
    """).pl()
    
    if latest_df.is_empty():
        return {}
        
    latest = latest_df.to_dicts()[0]
    
    # 2. Fetch record from 24h ago (or closest to it)
    past_df = conn.execute(f"""
        SELECT open_interest, close
        FROM futures_data
        WHERE underlying = '{underlying}'
          AND timestamp <= ?
        ORDER BY timestamp DESC
        LIMIT 1
    """, (one_day_ago,)).pl()
    
    if past_df.is_empty():
        # Fall back to the absolute oldest record in the database for the initial launch day
        past_df = conn.execute(f"""
            SELECT open_interest, close
            FROM futures_data
            WHERE underlying = '{underlying}'
            ORDER BY timestamp ASC
            LIMIT 1
        """).pl()
        
    past_oi = latest["open_interest"]
    past_price = latest["close"]
    if not past_df.is_empty():
        past = past_df.to_dicts()[0]
        past_oi = past["open_interest"]
        past_price = past["close"]
        
    # 3. Fetch accumulated liquidations in last 24h
    liq_df = conn.execute(f"""
        SELECT SUM(liquidation_long) as total_long, SUM(liquidation_short) as total_short
        FROM futures_data
        WHERE underlying = '{underlying}'
          AND timestamp >= ?
    """, (one_day_ago,)).pl()
    
    liq_long = 0.0
    liq_short = 0.0
    if not liq_df.is_empty():
        liq = liq_df.to_dicts()[0]
        liq_long = liq["total_long"] or 0.0
        liq_short = liq["total_short"] or 0.0
        
    # 4. Fetch 24h High and Low
    range_df = conn.execute(f"""
        SELECT MAX(high) as high_24h, MIN(low) as low_24h
        FROM futures_data
        WHERE underlying = '{underlying}'
          AND timestamp >= ?
    """, (one_day_ago,)).pl()
    
    high_24h = latest["close"]
    low_24h = latest["close"]
    if not range_df.is_empty():
        rng = range_df.to_dicts()[0]
        if rng["high_24h"] is not None:
            high_24h = rng["high_24h"]
        if rng["low_24h"] is not None:
            low_24h = rng["low_24h"]
        
    price_change_pct = ((latest["close"] - past_price) / past_price) * 100 if past_price > 0 else 0.0
    oi_change_pct = ((latest["open_interest"] - past_oi) / past_oi) * 100 if past_oi > 0 else 0.0
    
    return {
        "underlying": underlying,
        "price": latest["close"],
        "price_change_24h": price_change_pct,
        "high_24h": high_24h,
        "low_24h": low_24h,
        "open_interest": latest["open_interest"],
        "open_interest_change_24h": oi_change_pct,
        "funding_rate": latest["funding_rate"] * 100,  # percent
        "predicted_funding": latest["predicted_funding"] * 100,  # percent
        "liq_long_24h": liq_long,
        "liq_short_24h": liq_short,
        "long_short_ratio": latest["long_short_ratio"]
    }

def get_options_summary(conn, underlying: str) -> dict:
    """Computes options metrics (ATM IV, Skew, Term Structure, Max Pain) from option_chains."""
    # 1. Get spot price from latest futures data
    spot_df = conn.execute(f"SELECT close FROM futures_data WHERE underlying = '{underlying}' ORDER BY timestamp DESC LIMIT 1").pl()
    if spot_df.is_empty():
        return {}
    spot = spot_df.to_dicts()[0]["close"]
    
    # 2. Get latest options snapshot
    latest_ts_df = conn.execute(f"SELECT MAX(timestamp) as ts FROM option_chains WHERE underlying = '{underlying}'").pl()
    if latest_ts_df.is_empty() or latest_ts_df.to_dicts()[0]["ts"] is None:
        return {}
    latest_ts = latest_ts_df.to_dicts()[0]["ts"]
    
    options_df = conn.execute("""
        SELECT instrument_name, expiry, strike, option_type, mark_price, mark_iv, open_interest, volume, delta
        FROM option_chains
        WHERE underlying = ? AND timestamp = ?
    """, (underlying, latest_ts)).pl()
    
    if options_df.is_empty():
        return {}
        
    # ATM IV Calculation: Expiry between 14 and 45 days, strike closest to spot
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    options_with_days = options_df.with_columns(
        ((pl.col("expiry") - now).dt.total_days()).alias("days_to_expiry")
    )
    
    atm_candidates = options_with_days.filter(
        (pl.col("days_to_expiry") >= 14) & (pl.col("days_to_expiry") <= 45)
    )
    
    atm_iv = 0.0
    if not atm_candidates.is_empty():
        # Sort by distance to strike
        atm_candidates = atm_candidates.with_columns(
            (pl.col("strike") - spot).abs().alias("strike_dist")
        ).sort("strike_dist")
        
        # Take the top closest options (e.g. Call and Put of the closest strike)
        closest_strike = atm_candidates[0, "strike"]
        closest_strike_opts = atm_candidates.filter(pl.col("strike") == closest_strike)
        # Average mark_iv
        atm_iv = closest_strike_opts["mark_iv"].mean()
        
    # IV Rank Calculation: Lookback 90 days from daily_options_summary
    iv_rank = 0.0
    hist_df = conn.execute("""
        SELECT atm_iv FROM daily_options_summary
        WHERE underlying = ? AND date >= CURRENT_DATE - INTERVAL '90 days'
        ORDER BY date ASC
    """, (underlying,)).pl()
    
    if not hist_df.is_empty():
        ivs = hist_df["atm_iv"].drop_nans().drop_nulls()
        # Include current ATM IV in the calculation
        all_ivs = list(ivs) + [atm_iv]
        min_iv = min(all_ivs)
        max_iv = max(all_ivs)
        if max_iv > min_iv:
            iv_rank = ((atm_iv - min_iv) / (max_iv - min_iv)) * 100
        else:
            iv_rank = 50.0 # neutral default
            
    # Term Structure: Find ATM IV for different expiries
    # Expiries categorized as: Near (<= 14 days), Monthly (15-35 days), Quarterly (36-65 days)
    term_structure = {}
    expiries = options_with_days.select("expiry").unique().sort("expiry")
    for exp_dt in expiries["expiry"]:
        exp_opts = options_with_days.filter(pl.col("expiry") == exp_dt)
        days = (exp_dt - now).days
        # Find ATM option for this expiry
        exp_opts = exp_opts.with_columns(
            (pl.col("strike") - spot).abs().alias("strike_dist")
        ).sort("strike_dist")
        if not exp_opts.is_empty():
            closest_strike = exp_opts[0, "strike"]
            term_structure[f"{days}d"] = exp_opts.filter(pl.col("strike") == closest_strike)["mark_iv"].mean()

    # 25-Delta Skew Calculation: Put IV - Call IV for the next major monthly expiry (days between 14 and 45)
    skew_25d = 0.0
    target_expiry = None
    
    monthly_expiries = options_with_days.filter(
        (pl.col("days_to_expiry") >= 14) & (pl.col("days_to_expiry") <= 45)
    ).select("expiry").unique().sort("expiry")
    
    if not monthly_expiries.is_empty():
        target_expiry = monthly_expiries[0, "expiry"]
        expiry_opts = options_with_days.filter(pl.col("expiry") == target_expiry)
        
        # Put 25-delta (delta closest to -0.25)
        puts = expiry_opts.filter(pl.col("option_type") == "P")
        puts_25d = puts.with_columns(
            (pl.col("delta") - (-0.25)).abs().alias("delta_dist")
        ).sort("delta_dist")
        
        # Call 25-delta (delta closest to 0.25)
        calls = expiry_opts.filter(pl.col("option_type") == "C")
        calls_25d = calls.with_columns(
            (pl.col("delta") - 0.25).abs().alias("delta_dist")
        ).sort("delta_dist")
        
        if not puts_25d.is_empty() and not calls_25d.is_empty():
            put_iv = puts_25d[0, "mark_iv"]
            call_iv = calls_25d[0, "mark_iv"]
            skew_25d = put_iv - call_iv

    # Max Pain Calculation for the next major expiry
    max_pain_strike = 0.0
    if target_expiry:
        expiry_opts = options_with_days.filter(pl.col("expiry") == target_expiry)
        strikes = expiry_opts.select("strike").unique().sort("strike")["strike"]
        
        min_pain = float("inf")
        for k in strikes:
            pain = 0.0
            # Calculate pain for calls (options lose value when spot <= strike, pain is buyer payoff)
            # Pain is calculated as OI * Max(0, spot_at_expiry - strike) for Calls
            # and OI * Max(0, strike - spot_at_expiry) for Puts
            # If spot expires at k:
            for opt in expiry_opts.to_dicts():
                oi = opt["open_interest"] or 0.0
                strike = opt["strike"]
                if opt["option_type"] == "C":
                    pain += oi * max(0.0, k - strike)
                else:
                    pain += oi * max(0.0, strike - k)
            
            if pain < min_pain:
                min_pain = pain
                max_pain_strike = k

    # Put/Call Ratio (Open Interest based)
    total_put_oi = options_df.filter(pl.col("option_type") == "P")["open_interest"].sum() or 0.0
    total_call_oi = options_df.filter(pl.col("option_type") == "C")["open_interest"].sum() or 0.0
    pcr_oi = total_put_oi / total_call_oi if total_call_oi > 0 else 0.0

    return {
        "underlying": underlying,
        "atm_iv": atm_iv,
        "iv_rank": iv_rank,
        "skew_25d": skew_25d,
        "term_structure": term_structure,
        "max_pain": max_pain_strike,
        "put_call_ratio": pcr_oi,
        "total_volume": options_df["volume"].sum() or 0.0,
        "total_oi": options_df["open_interest"].sum() or 0.0
    }

def update_daily_summary(conn):
    """Saves today's final computed ATM IV to daily_options_summary."""
    today = datetime.now(timezone.utc).date()
    
    for currency in ["BTC", "ETH", "SOL"]:
        opts = get_options_summary(conn, currency)
        if not opts or "atm_iv" not in opts or opts["atm_iv"] == 0.0:
            continue
            
        conn.execute("""
            INSERT OR REPLACE INTO daily_options_summary (
                date, underlying, atm_iv, put_call_ratio, skew_25d, open_interest, volume
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            today, currency, opts["atm_iv"], opts["put_call_ratio"],
            opts["skew_25d"], opts["total_oi"], opts["total_volume"]
        ))
        
    conn.commit()
    print("Daily options summary table updated for today.")

def classify_profile_shape(profile_df, poc, val, vah) -> dict:
    """Classifies the profile shape as D-shape, P-shape, b-shape, or B-shape (Double Distribution)."""
    if profile_df is None or profile_df.is_empty():
        return {"shape": "Unknown", "desc": "No profile data available."}
        
    bins = profile_df["bins"].to_list()
    vols = profile_df["volume"].to_list()
    
    n = len(bins)
    if n < 5:
        return {"shape": "D-shape", "desc": "Balanced distribution (insufficient resolution for detailed shape)."}
        
    low_price = bins[0]
    high_price = bins[-1]
    price_range = high_price - low_price
    
    if price_range == 0:
        return {"shape": "D-shape", "desc": "Flat profile."}
        
    # Relative position of POC
    poc_rel = (poc - low_price) / price_range if poc is not None else 0.5
    
    # Divide into thirds
    third = price_range / 3.0
    t1 = low_price + third
    t2 = low_price + 2 * third
    
    vol_lower = 0.0
    vol_middle = 0.0
    vol_upper = 0.0
    
    for b, v in zip(bins, vols):
        if b < t1:
            vol_lower += v
        elif b < t2:
            vol_middle += v
        else:
            vol_upper += v
            
    total_vol = vol_lower + vol_middle + vol_upper
    if total_vol == 0:
        return {"shape": "Unknown", "desc": "No volume detected."}
        
    p_lower = vol_lower / total_vol
    p_middle = vol_middle / total_vol
    p_upper = vol_upper / total_vol
    
    # Check for Double Distribution (B-shape)
    is_double_dist = False
    if p_middle < p_lower and p_middle < p_upper and p_middle < 0.25:
        is_double_dist = True
        
    if is_double_dist:
        return {
            "shape": "B-shape (Double Distribution)",
            "desc": "Two separate balance areas separated by a low-volume zone. Indicates transition/breakout from one value area to another."
        }
    elif p_upper > 1.5 * p_lower and poc_rel > 0.55:
        return {
            "shape": "P-shape",
            "desc": "Thin volume at the bottom, consolidation/heavy volume at the top. Typically bullish (short-covering rally or consolidation after an upward trend)."
        }
    elif p_lower > 1.5 * p_upper and poc_rel < 0.45:
        return {
            "shape": "b-shape",
            "desc": "Thin volume at the top, consolidation/heavy volume at the bottom. Typically bearish (long liquidation or consolidation after a downward trend)."
        }
    else:
        return {
            "shape": "D-shape",
            "desc": "Balanced profile with volume concentrated in the middle or evenly distributed. Indicates bracketed, rangebound market balance."
        }

def get_profile_summary(conn, underlying: str, lookback_days: int = 1) -> dict:
    """Computes TPO and Volume Profile metrics, including EMA26/99 calculations, nearness indicators, and profile shapes using Polars."""
    # 1. Fetch OHLCV data from DuckDB directly into Polars with 3 extra days for EMA warm-up
    query = f"""
        SELECT timestamp, open, high, low, close, volume
        FROM futures_data
        WHERE underlying = '{underlying}'
          AND low > 0.0
          AND timestamp >= CURRENT_TIMESTAMP - INTERVAL '{lookback_days + 3} days'
        ORDER BY timestamp ASC
    """
    df = conn.execute(query).pl()
    
    if df.is_empty():
        return {}
        
    df = df.sort("timestamp")
    
    # Calculate EMA26 and EMA99 on the 15m candles
    df = df.with_columns([
        pl.col("close").ewm_mean(span=26, adjust=False).alias("ema26"),
        pl.col("close").ewm_mean(span=99, adjust=False).alias("ema99")
    ])
    
    # Extract latest price and EMAs
    latest_row = df.tail(1)
    close_price = latest_row["close"][0]
    ema26_val = latest_row["ema26"][0]
    ema99_val = latest_row["ema99"][0]
    
    # Slice the dataframe to get only the lookback_days for profile calculations
    latest_ts = df["timestamp"].max()
    if latest_ts is None:
        return {}
    cutoff_ts = latest_ts - timedelta(days=lookback_days)
    df_profile = df.filter(pl.col("timestamp") >= cutoff_ts)
    
    # Capture data range for anchoring info
    data_start = df_profile["timestamp"].min()
    data_end = df_profile["timestamp"].max()
    candle_count = len(df_profile)

    # Require a minimum candle count (at least 30% of expected lookback window, and at least 48 candles to prevent mathematical collapse)
    min_required = max(48, int(lookback_days * 24 * 4 * 0.3))
    if len(df_profile) < min_required:
        return {
            "status": "Insufficient data",
            "candles_count": len(df_profile),
            "required_candles": min_required
        }
    # Calculate VWAP over the lookback window using Typical Price
    total_volume_sum = df_profile["volume"].sum()
    if total_volume_sum is not None and total_volume_sum > 0:
        typical_price = (df_profile["high"] + df_profile["low"] + df_profile["close"]) / 3.0
        vwap_val = (typical_price * df_profile["volume"]).sum() / total_volume_sum
    else:
        vwap_val = None

    # Determine scaling factor to convert small prices to clean integers for Polars int_ranges
    max_price = df_profile["high"].max()
    if max_price is None or max_price == 0.0:
        return {}
        
    scale_factor = 1.0
    if max_price < 0.1:
        scale_factor = 1000000.0  # Scale up sub-dollar coins by 1M
    elif max_price < 10.0:
        scale_factor = 10000.0     # Scale up low-priced coins by 10k
    elif max_price < 100.0:
        scale_factor = 1000.0      # Scale up by 1k
    elif max_price < 1000.0:
        scale_factor = 100.0       # Scale up by 100
        
    # Apply scaling and cast to Int64
    df_profile = df_profile.with_columns([
        (pl.col("low") * scale_factor).alias("low_scaled"),
        (pl.col("high") * scale_factor).alias("high_scaled")
    ])
    
    # Calculate a dynamic bin size for the scaled prices
    min_low_scaled = df_profile["low_scaled"].min()
    max_high_scaled = df_profile["high_scaled"].max()
    scaled_range = max_high_scaled - min_low_scaled
    
    # Target roughly 50 bins in the range, minimum bin size is 1
    bin_size = max(1, int(scaled_range / 50))
    
    # Calculate first and last bin using the scaled prices
    df_profile = df_profile.with_columns([
        ((pl.col("low_scaled") / bin_size).floor() * bin_size).cast(pl.Int64).alias("first_bin"),
        ((pl.col("high_scaled") / bin_size).floor() * bin_size).cast(pl.Int64).alias("last_bin")
    ])
    
    # Generate list of bins for each candle using pl.int_ranges
    df_profile = df_profile.with_columns(
        pl.int_ranges(pl.col("first_bin"), pl.col("last_bin") + bin_size, bin_size).alias("bins")
    )
    df_profile = df_profile.with_columns(
        pl.col("bins").list.len().alias("num_bins")
    )
    
    # Explode the bins to create a row per bin for each candle
    df_exploded = df_profile.explode("bins")
    
    # Distribute volume and TPO
    df_exploded = df_exploded.with_columns([
        (pl.col("volume") / pl.col("num_bins")).alias("allocated_volume"),
        pl.lit(1.0).alias("allocated_tpo")
    ])
    
    # Aggregate to get profile
    profile = df_exploded.group_by("bins").agg([
        pl.col("allocated_tpo").sum().alias("tpo_count"),
        pl.col("allocated_volume").sum().alias("volume")
    ]).sort("bins")
    
    if profile.is_empty():
        return {}
        
    # Calculate POCs (Point of Control)
    poc_tpo_row = profile.filter(pl.col("tpo_count") == pl.col("tpo_count").max())
    poc_vol_row = profile.filter(pl.col("volume") == pl.col("volume").max())
    
    poc_tpo_scaled = poc_tpo_row["bins"][0] if not poc_tpo_row.is_empty() else None
    poc_vol_scaled = poc_vol_row["bins"][0] if not poc_vol_row.is_empty() else None
    
    # Value Area (70%) Calculation
    def get_value_area(prof_df, value_col, poc_val, pct=0.70):
        if prof_df.is_empty() or poc_val is None:
            return None, None
            
        bins = prof_df["bins"].to_list()
        values = prof_df[value_col].to_list()
        
        total_val = sum(values)
        if total_val == 0:
            return None, None
            
        target_val = total_val * pct
        
        try:
            poc_idx = bins.index(poc_val)
        except ValueError:
            return None, None
            
        lower_idx = poc_idx
        upper_idx = poc_idx
        accum_val = values[poc_idx]
        
        n = len(bins)
        
        while accum_val < target_val:
            # Check if we can expand in both directions
            can_go_up = upper_idx + 2 < n
            can_go_down = lower_idx - 2 >= 0
            
            if can_go_up and can_go_down:
                val_up = values[upper_idx + 1] + values[upper_idx + 2]
                val_down = values[lower_idx - 1] + values[lower_idx - 2]
                
                if val_up >= val_down:
                    accum_val += values[upper_idx + 1] + values[upper_idx + 2]
                    upper_idx += 2
                else:
                    accum_val += values[lower_idx - 1] + values[lower_idx - 2]
                    lower_idx -= 2
            elif can_go_up:
                accum_val += values[upper_idx + 1]
                upper_idx += 1
            elif can_go_down:
                accum_val += values[lower_idx - 1]
                lower_idx -= 1
            else:
                break
                
        return bins[lower_idx], bins[upper_idx]

    tpo_val_scaled, tpo_vah_scaled = get_value_area(profile, "tpo_count", poc_tpo_scaled)
    vol_val_scaled, vol_vah_scaled = get_value_area(profile, "volume", poc_vol_scaled)
    
    # Scale back to original float scale
    poc_tpo = poc_tpo_scaled / scale_factor if poc_tpo_scaled is not None else None
    poc_vol = poc_vol_scaled / scale_factor if poc_vol_scaled is not None else None
    tpo_val = tpo_val_scaled / scale_factor if tpo_val_scaled is not None else None
    tpo_vah = tpo_vah_scaled / scale_factor if tpo_vah_scaled is not None else None
    vol_val = vol_val_scaled / scale_factor if vol_val_scaled is not None else None
    vol_vah = vol_vah_scaled / scale_factor if vol_vah_scaled is not None else None
    
    # Convert bins in profile DataFrame back to float scale
    profile = profile.with_columns(
        (pl.col("bins") / scale_factor).alias("bins")
    )
    
    # Low Volume Nodes (LVN) Calculation
    # Find local minima in the volume profile
    bins_list = profile["bins"].to_list()
    vol_list = profile["volume"].to_list()
    lvn_candidates = []
    
    n = len(bins_list)
    if n > 2:
        mean_vol = sum(vol_list) / n
        for i in range(1, n - 1):
            # Check if it's a local minimum (valley) and below average volume
            if vol_list[i] < vol_list[i-1] and vol_list[i] < vol_list[i+1]:
                if vol_list[i] < mean_vol:
                    lvn_candidates.append((bins_list[i], vol_list[i]))
                    
    # Sort candidates by volume (lowest volume first)
    lvn_candidates.sort(key=lambda x: x[1])
    lvns = [x[0] for x in lvn_candidates[:3]] # Top 3 most prominent LVNs
    
    # High Volume Nodes (HVN) Calculation
    # Find local maxima in the volume profile (peaks)
    hvn_candidates = []
    if n > 2:
        for i in range(1, n - 1):
            # Local peak (not the POC itself)
            if vol_list[i] > vol_list[i-1] and vol_list[i] > vol_list[i+1]:
                if bins_list[i] != poc_vol:
                    hvn_candidates.append((bins_list[i], vol_list[i]))
                    
    # Sort candidates by volume descending (highest volume first)
    hvn_candidates.sort(key=lambda x: x[1], reverse=True)
    hvns = [x[0] for x in hvn_candidates[:3]]

    # Profile Shape Classification
    shape_dict = classify_profile_shape(profile, poc_vol, vol_val, vol_vah)
    
    # Technical Analysis Nearness Signal (Thesis: Price near EMAs + POC confluence)
    ta_signal = "Neutral"
    ta_desc = "Price and EMAs are dispersed away from the POC."
    if poc_vol is not None and close_price is not None and ema26_val is not None and ema99_val is not None:
        diff_ema26_poc = abs(ema26_val - poc_vol) / poc_vol
        diff_ema99_poc = abs(ema99_val - poc_vol) / poc_vol
        diff_price_poc = abs(close_price - poc_vol) / poc_vol
        diff_price_ema26 = abs(close_price - ema26_val) / ema26_val
        diff_price_ema99 = abs(close_price - ema99_val) / ema99_val
        
        # Nearness threshold is 0.75%
        threshold = 0.0075
        
        is_ema26_near_poc = diff_ema26_poc <= threshold
        is_ema99_near_poc = diff_ema99_poc <= threshold
        is_price_near_poc = diff_price_poc <= threshold
        is_price_near_ema26 = diff_price_ema26 <= threshold
        is_price_near_ema99 = diff_price_ema99 <= threshold
        
        if is_price_near_poc and is_ema26_near_poc and is_ema99_near_poc and is_price_near_ema26 and is_price_near_ema99:
            ta_signal = "🔥 HIGH CONFLUENCE ENTRY"
            ta_desc = "Price, EMAs (26/99), and POC are tightly coiled. High potential for a massive expansion/breakout. Setup for long or short depending on breakout direction."
        elif is_price_near_poc and (is_price_near_ema26 or is_price_near_ema99):
            ta_signal = "⚡ STRONG CONFLUENCE"
            ta_desc = f"Price is trading near the POC while testing {'EMA26' if is_price_near_ema26 else 'EMA99'}. Strong value area entry zone."
        elif is_ema26_near_poc and is_ema99_near_poc:
            ta_signal = "⏳ POTENTIAL ENTRY (EMA Pullback)"
            ta_desc = "EMAs are coiled near the POC, but price has drifted. Watch for a pullback to the POC/EMA zone for a low-risk entry."
        else:
            ta_signal = "Neutral (No Confluence)"
            ta_desc = "Price, EMAs, and POC are dispersed. No clear confluence entry zone at current price."

    # --- Enhanced trade levels for high confluence alerts ---
    va_width = (vol_vah - vol_val) if (vol_vah is not None and vol_val is not None and vol_vah > vol_val) else None

    trigger_long = vol_vah + va_width * 0.15 if va_width is not None and vol_vah is not None else None
    trigger_short = vol_val - va_width * 0.15 if va_width is not None and vol_val is not None else None

    t1_long = vol_vah + va_width * 0.5 if va_width is not None and vol_vah is not None else None
    t2_long = vol_vah + va_width * 1.0 if va_width is not None and vol_vah is not None else None
    t1_short = vol_val - va_width * 0.5 if va_width is not None and vol_val is not None else None
    t2_short = vol_val - va_width * 1.0 if va_width is not None and vol_val is not None else None

    rr_long_t1 = rr_long_t2 = rr_short_t1 = rr_short_t2 = None
    if trigger_long is not None and poc_vol is not None and trigger_long > poc_vol:
        risk_long = trigger_long - poc_vol
        if risk_long > 0:
            rr_long_t1 = round((t1_long - trigger_long) / risk_long, 1) if t1_long is not None and t1_long > trigger_long else None
            rr_long_t2 = round((t2_long - trigger_long) / risk_long, 1) if t2_long is not None and t2_long > trigger_long else None
    if trigger_short is not None and poc_vol is not None and trigger_short < poc_vol:
        risk_short = poc_vol - trigger_short
        if risk_short > 0:
            rr_short_t1 = round((trigger_short - t1_short) / risk_short, 1) if t1_short is not None and t1_short < trigger_short else None
            rr_short_t2 = round((trigger_short - t2_short) / risk_short, 1) if t2_short is not None and t2_short < trigger_short else None

    bias_parts = []
    if close_price is not None and vwap_val is not None:
        if close_price < vwap_val:
            bias_parts.append("price below VWAP")
        else:
            bias_parts.append("price above VWAP")
    if ema26_val is not None and ema99_val is not None:
        if ema26_val > ema99_val:
            bias_parts.append("EMAs bullish (26>99)")
        elif ema26_val < ema99_val:
            bias_parts.append("EMAs bearish (26<99)")
        else:
            bias_parts.append("EMAs flat")
    if vol_val is not None and vol_vah is not None and close_price is not None:
        va_mid = (vol_val + vol_vah) / 2
        if close_price < va_mid:
            bias_parts.append("price in lower VA")
        elif close_price > va_mid:
            bias_parts.append("price in upper VA")
        else:
            bias_parts.append("price at VA mid")

    bearish_signals = 0
    bullish_signals = 0
    if close_price is not None and vwap_val is not None:
        if close_price < vwap_val: bearish_signals += 1
        else: bullish_signals += 1
    if ema26_val is not None and ema99_val is not None:
        if ema26_val < ema99_val: bearish_signals += 1
        elif ema26_val > ema99_val: bullish_signals += 1
    if close_price is not None and vol_val is not None and vol_vah is not None:
        va_mid = (vol_val + vol_vah) / 2
        if close_price < va_mid: bearish_signals += 1
        elif close_price > va_mid: bullish_signals += 1

    if bearish_signals > bullish_signals:
        bias_lean = "Slight short lean"
    elif bullish_signals > bearish_signals:
        bias_lean = "Slight long lean"
    else:
        bias_lean = "Neutral lean"
    bias_assessment = f"{bias_lean} ({', '.join(bias_parts)})" if bias_parts else bias_lean

    # --- Directional bias and confidence scoring ---
    directional_bias = "neutral"
    conviction = "LOW"
    confidence_score = 0
    confirmations_list = []

    if ta_signal == "🔥 HIGH CONFLUENCE ENTRY":
        if ema26_val is not None and ema99_val is not None:
            ema_gap = abs(ema26_val - ema99_val) / max(ema99_val, 1e-10)
            if ema_gap < 0.001:
                directional_bias = "neutral"
            elif ema26_val > ema99_val:
                directional_bias = "long"
            else:
                directional_bias = "short"

        if directional_bias in ("long", "short"):
            is_bull = directional_bias == "long"
            score = 0
            confs = []

            # 1. Price vs VWAP
            if vwap_val is not None and close_price is not None:
                if (is_bull and close_price > vwap_val) or (not is_bull and close_price < vwap_val):
                    score += 1
                    confs.append("price-VWAP alignment")
                else:
                    score -= 1

            # 2. Price vs VA midpoint
            if vol_val is not None and vol_vah is not None and close_price is not None:
                va_mid = (vol_val + vol_vah) / 2
                if (is_bull and close_price > va_mid) or (not is_bull and close_price < va_mid):
                    score += 1
                    confs.append("price-VA alignment")
                else:
                    score -= 1

            # 3. Profile shape alignment
            shape_name = shape_dict.get("shape", "")
            if is_bull:
                if shape_name == "P-shape":
                    score += 1
                    confs.append("P-shape (bullish)")
                elif shape_name in ("b-shape", "B-shape (Double Distribution)"):
                    score -= 1
            else:
                if shape_name == "b-shape":
                    score += 1
                    confs.append("b-shape (bearish)")
                elif shape_name in ("P-shape", "B-shape (Double Distribution)"):
                    score -= 1

            # 4. Price vs POC
            if poc_vol is not None and close_price is not None:
                if (is_bull and close_price >= poc_vol) or (not is_bull and close_price <= poc_vol):
                    score += 1
                    confs.append("price-POC alignment")
                else:
                    score -= 1

            # 5. Volume surge
            if volume_surge:
                score += 1
                confs.append("volume surge")

            confidence_score = score
            confirmations_list = confs

            if score >= 3:
                conviction = "HIGH"
            elif score >= 1:
                conviction = "MODERATE"
            else:
                conviction = "LOW"

            # Update ta_desc with directional context
            if directional_bias == "long":
                ta_desc = "Price, EMAs (26/99), and POC are tightly coiled within an uptrend (EMA26>EMA99). High potential for a bullish breakout continuation. Prioritize long entries."
            else:
                ta_desc = "Price, EMAs (26/99), and POC are tightly coiled within a downtrend (EMA26<EMA99). High potential for a bearish breakdown continuation. Prioritize short entries."
        else:
            conviction = "LOW"

    now_utc = datetime.now(timezone.utc)
    staleness_mins = round((now_utc - data_end).total_seconds() / 60) if data_end is not None else None

    avg_candle_volume = df_profile["volume"].mean() if not df_profile.is_empty() else None
    last_candle_volume = df_profile.tail(1)["volume"][0] if not df_profile.is_empty() else None
    volume_surge = (last_candle_volume > avg_candle_volume * 1.2) if (last_candle_volume is not None and avg_candle_volume is not None and avg_candle_volume > 0) else False

    return {
        "underlying": underlying,
        "tpo_poc": poc_tpo,
        "tpo_val": tpo_val,
        "tpo_vah": tpo_vah,
        "volume_poc": poc_vol,
        "val": vol_val,
        "vah": vol_vah,
        "hvns": hvns,
        "lvns": lvns,
        "profile_df": profile,
        "vwap": vwap_val,
        "ema26": ema26_val,
        "ema99": ema99_val,
        "close": close_price,
        "profile_shape": shape_dict["shape"],
        "profile_shape_desc": shape_dict["desc"],
        "ta_signal": ta_signal,
        "ta_desc": ta_desc,
        "data_start": data_start,
        "data_end": data_end,
        "candle_count": candle_count,
        "trigger_long": trigger_long,
        "trigger_short": trigger_short,
        "stop_anchor": poc_vol,
        "t1_long": t1_long,
        "t2_long": t2_long,
        "t1_short": t1_short,
        "t2_short": t2_short,
        "rr_long_t1": rr_long_t1,
        "rr_long_t2": rr_long_t2,
        "rr_short_t1": rr_short_t1,
        "rr_short_t2": rr_short_t2,
        "bias_assessment": bias_assessment,
        "bias_lean": bias_lean,
        "staleness_mins": staleness_mins,
        "avg_candle_volume": avg_candle_volume,
        "volume_surge": volume_surge,
        "directional_bias": directional_bias,
        "conviction": conviction,
        "confidence_score": confidence_score,
        "confirmations": confirmations_list
    }

def generate_ascii_profile(profile_df, poc, val, vah, num_bars=12) -> str:
    """Generates a text-based visual histogram of the volume profile."""
    if profile_df.is_empty() or poc is None:
        return "No profile data available."
        
    bins = profile_df["bins"].to_list()
    
    min_b, max_b = min(bins), max(bins)
    if min_b == max_b:
        return f"${min_b:.6f} | █ (POC)" if min_b < 1.0 else f"${min_b:,.2f} | █ (POC)"
        
    step = (max_b - min_b) / num_bars
    
    binned_data = []
    for i in range(num_bars):
        b_start = min_b + i * step
        b_end = b_start + step
        
        # Filter bins in this range
        segment_df = profile_df.filter((pl.col("bins") >= b_start) & (pl.col("bins") < b_end))
        seg_vol = segment_df["volume"].sum() if not segment_df.is_empty() else 0.0
        
        # Midpoint of the bin for label
        mid_price = (b_start + b_end) / 2
        binned_data.append((mid_price, seg_vol))
        
    max_vol = max([x[1] for x in binned_data]) if binned_data else 0
    if max_vol == 0:
        return "No volume in profile."
        
    lines = []
    for mid_price, vol in binned_data:
        # Determine bar length (max 15 characters to look clean on mobile Telegram)
        bar_len = int((vol / max_vol) * 15) if max_vol > 0 else 0
        bar = "█" * bar_len if bar_len > 0 else " "
        
        # Add labels for POC, VAL, VAH
        labels = []
        if abs(mid_price - poc) <= step/2:
            labels.append("POC")
        if val is not None and vah is not None:
            if abs(mid_price - val) <= step/2:
                labels.append("VAL")
            if abs(mid_price - vah) <= step/2:
                labels.append("VAH")
                
        label_str = " ← " + "+".join(labels) if labels else ""
        
        # Format label precision depending on price magnitude
        if mid_price >= 1.0:
            price_str = f"${mid_price:,.2f}" if mid_price < 10000.0 else f"${round(mid_price):,.0f}"
        else:
            price_str = f"${mid_price:.6f}"
            
        lines.append(f"{price_str} | {bar}{label_str}")
        
    lines.reverse()
    return "\n".join(lines)

def generate_market_brief() -> str:
    """Generates the Markdown brief text for Telegram."""
    conn = config.get_db_connection(read_only=True)
    try:
        # Generate briefs for BTC and ETH
        briefs = []
        for currency in ["BTC", "ETH", "SOL"]:
            fut = get_futures_summary(conn, currency)
            opt = get_options_summary(conn, currency)
            
            if not fut or not opt:
                continue
                
            # Formatting Term Structure
            ts_str = ", ".join([f"{k}: {v:.1f}%" for k, v in list(opt["term_structure"].items())[:4]])
            
            # Sentiment Synthesis
            skew_val = opt["skew_25d"]
            skew_desc = "Put Skew (Bearish Hedging)" if skew_val > 1.5 else ("Call Skew (Bullish)" if skew_val < -1.5 else "Neutral Skew")
            
            funding_desc = "High/Leveraged (Bullish Perp Premium)" if fut["funding_rate"] > 0.03 else ("Negative/Discount" if fut["funding_rate"] < -0.01 else "Neutral")
            
            # Fetch Volume Profile
            prof_line = ""
            try:
                prof = get_profile_summary(conn, currency, lookback_days=1)
                if prof:
                    if prof.get("status") == "Insufficient data":
                        prof_line = f"\n• *Volume Profile:* Insufficient historical data ({prof.get('candles_count', 0)}/{prof.get('required_candles', 48)} candles). Populating database..."
                    else:
                        spot = fut["price"]
                        vol_val = prof["val"]
                        vol_vah = prof["vah"]
                        vol_poc = prof["volume_poc"]
                        hvns = prof.get("hvns", [])
                        lvns = prof.get("lvns", [])
                        
                        def fmt_p(val):
                            if val is None:
                                return "N/A"
                            if val < 1.0:
                                return f"${val:.6f}"
                            return f"${val:,.2f}" if val < 10000.0 else f"${round(val):,.0f}"

                        vol_poc_str = fmt_p(vol_poc)
                        vol_val_str = fmt_p(vol_val)
                        vol_vah_str = fmt_p(vol_vah)
                        
                        hvn_list = [vol_poc] + hvns
                        hvns_str = ", ".join([fmt_p(x) for x in hvn_list[:3]])
                        lvns_str = ", ".join([fmt_p(x) for x in lvns[:2]]) if lvns else "N/A"
                        
                        shape_str = prof.get("profile_shape", "D-shape")
                        shape_desc = prof.get("profile_shape_desc", "")
                        ta_signal = prof.get("ta_signal", "Neutral")
                        ta_desc = prof.get("ta_desc", "")
                        
                        if vol_val is not None and vol_vah is not None:
                            if spot > vol_vah:
                                pos_desc = "Spot > VAH (Bullish)"
                            elif spot < vol_val:
                                pos_desc = "Spot < VAL (Bearish)"
                            else:
                                pos_desc = "Spot in VA (Rangebound)"
                            prof_line = (
                                f"\n• *Volume Profile (24h Rolling - Floating Anchor):*\n"
                                f"  - POC: {vol_poc_str} | VAL: {vol_val_str} | VAH: {vol_vah_str} ({pos_desc})\n"
                                f"  - VWAP: {fmt_p(prof.get('vwap'))}\n"
                                f"  - HVNs: {hvns_str} | LVNs: {lvns_str}\n"
                                f"  - Shape: *{shape_str}* ({shape_desc})\n"
                                f"  - TA Confluence: *{ta_signal}*\n"
                                f"    _{ta_desc}_"
                            )
                        else:
                            prof_line = (
                                f"\n• *Volume Profile (24h Rolling - Floating Anchor):*\n"
                                f"  - POC: {vol_poc_str} | VWAP: {fmt_p(prof.get('vwap'))}\n"
                                f"  - Shape: *{shape_str}* ({shape_desc})\n"
                                f"  - TA Confluence: *{ta_signal}*\n"
                                f"    _{ta_desc}_"
                            )
            except Exception as e:
                print(f"Error generating profile summary for {currency}: {e}")
            
            # Logic for synthesis signal
            synthesis = ""
            if fut["funding_rate"] > 0.03 and opt["atm_iv"] < 45.0:
                synthesis = "High perp premium + Low options IV → Buy underpriced OTM Calls to lever upside, or run cash-and-carry."
            elif fut["funding_rate"] > 0.03 and opt["atm_iv"] > 60.0:
                synthesis = "Overheated perps + Elevated options IV → Look to sell Call spreads or write covered calls (short premium)."
            elif skew_val > 5.0 and fut["price_change_24h"] < -3.0:
                synthesis = "High Put skew + Spot drop → Hedging demand peaking. Consider Put Spread collars or wait for vol crush to buy spot."
            else:
                synthesis = f"Markets rangebound. Neutral Perp Premium ({fut['funding_rate']:.3f}%) & ATM IV ({opt['atm_iv']:.1f}%). Strategy: Rangebound iron condors or passive accumulation."

            # Formatting spot price and 24h range
            spot_val = fut["price"]
            high_val = fut["high_24h"]
            low_val = fut["low_24h"]
            
            def fmt_spot(val):
                if val is None:
                    return "N/A"
                if val < 1.0:
                    return f"${val:.6f}"
                return f"${val:,.2f}" if val < 10000.0 else f"${round(val):,.0f}"

            spot_str = fmt_spot(spot_val)
            range_str = f"{fmt_spot(low_val)} - {fmt_spot(high_val)}"

            brief_md = f"""
*📊 {currency} Market Snapshot*
• *Underlying Spot:* {spot_str} ({fut["price_change_24h"]:+.2f}% 24h) | *24h Range:* {range_str}
• *Futures Open Interest:* ${(fut["open_interest"] * fut["price"])/1e9:.2f}B ({fut["open_interest_change_24h"]:+.2f}% 24h)
• *Perp Funding Rate:* {fut["funding_rate"]:.4f}% (Predicted: {fut["predicted_funding"]:.4f}%) | {funding_desc}
• *24h Liquidations:* Longs ${(fut["liq_long_24h"] * fut["price"])/1e6:.1f}M | Shorts ${(fut["liq_short_24h"] * fut["price"])/1e6:.1f}M
• *Long/Short Ratio:* {fut["long_short_ratio"]:.2f}{prof_line}

*Volatility & Options Context*
• *ATM IV (14-45d):* {opt["atm_iv"]:.1f}% | *IV Rank (90d):* {opt["iv_rank"]:.1f}%
• *25-Delta Skew:* {opt["skew_25d"]:+.2f}% | {skew_desc}
• *Put/Call OI Ratio:* {opt["put_call_ratio"]:.2f}
• *Next Expiry Max Pain:* ${opt["max_pain"]:,.0f}
• *Term Structure:* {ts_str}

*Synthesis & Strategy:*
_{synthesis}_
"""
            briefs.append(brief_md)
            
        if not briefs:
            return "⚠️ No market data available. Make sure ingestion is running."
            
        header = f"🚀 *BTC/ETH OPTIONS & FUTURES MARKET BRIEF*\n📅 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        footer = "\n_Disclaimer: Research-only, not financial advice._"
        return header + "\n" + "—" * 20 + "\n" + "\n".join(briefs) + footer
    finally:
        conn.close()

if __name__ == "__main__":
    # Test script output
    import json
    conn = config.get_db_connection(read_only=True)
    try:
        print("Testing futures summary:")
        print(json.dumps(get_futures_summary(conn, "BTC"), indent=2, default=str))
        print("Testing options summary:")
        print(json.dumps(get_options_summary(conn, "BTC"), indent=2, default=str))
        print("\nGenerated Brief:")
        print(generate_market_brief())
    finally:
        conn.close()
