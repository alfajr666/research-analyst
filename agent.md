# Options Research Analyst Agent Spec

This document details the configuration and operational spec for the **Options Research Analyst Agent**.

## 🤖 Agent Purpose
The agent operates as an analytical system monitoring crypto futures (via CoinAnalyze). Options market monitoring (via Deribit) is currently disabled. Its primary background function is detecting high-probability structural trend setups and delivering actionable entry signals directly to Telegram.

## 📈 Confluence Trading Strategy (15m EMA 99 Pullback)
The agent implements the **15m EMA 99 Pullback + 1h Accumulation Confluence** setup, designed to find low-risk entries at key macro levels.

### 1. Trend & Support/Resistance (15m)
- The agent tracks the **15m EMA 99** as the primary trend filter.
- **Long Trend:** 15m Close > EMA 99. Pullback is valid if the last closed 15m candle closed within **1%** of the EMA 99 (`0.0% to 1.0%` above it).
- **Short Trend:** 15m Close < EMA 99. Pullback is valid if the last closed 15m candle closed within **1%** of the EMA 99 (`0.0% to 1.0%` below it).

### 2. Micro Volume Spike / Consolidation (1h)
- While the coin is resting on its 15m EMA support/resistance, the agent monitors for **Volume Accumulation/Distribution**.
- Requires a volume spike **>= 1.5x** the recent average volume.
- Requires quiet price consolidation (1h absolute price change **<= 3.0%**).

### 3. Momentum Execution (15m Trigger)
- To avoid catching falling knives, the agent waits for the first closed 15m candle in the direction of the trend:
  - **Long:** 15m candle closes **Green** (`close > open`).
  - **Short:** 15m candle closes **Red** (`close < open`).

### 4. 4h Structural Filter & Volatility Targeting
- **4h Structural Alignment:** The 15m trigger is validated against the 4h EMA26 vs EMA99 structural trend. If the 15m entry conflicts with the higher-timeframe 4h structure, the setup is suppressed.
- **RSI Momentum Filter:** The 15m 14-period RSI is used to gauge momentum strength. A Long setup is suppressed if RSI < 50. A Short setup is suppressed if RSI > 50.
- **Dynamic TP/SL (ATR-based):** Stop Losses and Targets are sized dynamically using a 14-period 15m Average True Range (ATR). Stop Anchor is set at 2x ATR, T1 at 2x ATR, and T2 at 4x ATR. This mathematically enforces a consistent 2:1 risk-reward profile regardless of the prevailing volatility regime.

---

## 🛡️ HMM + TraderXO Dual VWAP Regime Signal (Daily)
The agent evaluates daily market structure using a combination of rolling VWAP zones and a statistical classifier:

### 1. Setup Detection (Dual VWAP + Closes)
*   **Dual VWAP Bias:** Computes a rolling 7-day (weekly) VWAP on daily bars and an 18-bar (3-day) rolling VWAP on 4-hour bars. Price must be above both for a **long setup** candidate, or below both for a **short setup** candidate.
*   **Acceptance Filter:** At least **4 of the last 5** daily closes must reside on the correct side of the weekly VWAP.

### 2. Conviction Scoring (Confluences)
When a setup is found, it is evaluated across a 6-point checklist to determine conviction (**HIGH** (score >= 4), **MODERATE** (score 2-3), or **LOW** (score 0-1)):
*   **HMM Regime Alignment (+2 / +1 / -1):** A 3-state Gaussian Hidden Markov Model fits log return, realized volatility, VWAP deviation, volume z-score, and high-low ranges on **300 one-hour bars** (≈ 12.5 days, sourced from 15m futures data). It scores +2 for high confidence trending in the setup direction, +1 for mild confidence trending, or -1 for ranging/high-vol.
*   **EMA Alignment (+1):** Daily EMA12 vs EMA25 aligned with setup direction.
*   **Perfect Acceptance (+1):** All 5 of the last 5 closes are on the correct side.
*   **VWAP Distance (+1):** Price is >= 0.5% beyond the 4h (3-day) VWAP.

---

## 🛠️ Monitor & Orchestration Daemons
All analytical routines run continuously via process managers or sequential pipeline triggers.

### Actionable Pullback Alerts
When the 15m Pullback gates align, the agent sends a Telegram alert with a defined **Entry Zone** (using local state tracker `accumulation_state.json` to suppress duplicates):
- **Long Entry Zone:** `[EMA 99, EMA 99 * 1.01]`
- **Short Entry Zone:** `[EMA 99 * 0.99, EMA 99]`

### Daily Regime Alerts
The regime signal script runs **once per calendar day** via `orchestrator.py`.
- **Data Source:** Daily OHLCV aggregated from the `futures_data` DB table (15m candles). HMM features computed on 1-hour bars from the same source. No external freqtrade feather files required.
- **Alert Trigger:** Dispatches a Telegram notification **only** when a setup newly transitions to **HIGH** conviction. Invalidation alerts (when a HIGH setup closes) are suppressed and logged silently to reduce noise.
- **DB Logging:** Logs all signals (LOW/MODERATE/HIGH/no_signal) to DuckDB in the `regime_signals` table.
- **15m Confluence Gate:** The daily regime conviction acts as a filter for 15m confluence alerts. Alerts with **HIGH or MODERATE** daily conviction pass through; LOW/no_signal are suppressed. This ensures 15m breakout alerts only fire when the macro regime confirms the intraday bias.
