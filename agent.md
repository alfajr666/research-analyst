# Options Research Analyst Agent Spec

This document details the configuration and operational spec for the **Options Research Analyst Agent**.

## 🤖 Agent Purpose
The agent operates as an analytical system monitoring crypto futures (via CoinAnalyze) and options markets (via Deribit). Its primary background function is detecting high-probability structural trend setups and delivering actionable entry signals directly to Telegram.

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

---

## 🛡️ HMM + TraderXO Dual VWAP Regime Signal (Daily)
The agent evaluates daily market structure using a combination of rolling VWAP zones and a statistical classifier:

### 1. Setup Detection (Dual VWAP + Closes)
*   **Dual VWAP Bias:** Computes a rolling 7-day (weekly) and 30-day (monthly) VWAP. Price must be above both for a **long setup** candidate, or below both for a **short setup** candidate.
*   **Acceptance Filter:** At least **4 of the last 5** daily closes must reside on the correct side of the weekly VWAP.

### 2. Conviction Scoring (Confluences)
When a setup is found, it is evaluated across a 6-point checklist to determine conviction (**HIGH** (score >= 4), **MODERATE** (score 2-3), or **LOW** (score 0-1)):
*   **HMM Regime Alignment (+2 / +1 / -1):** A 3-state Gaussian Hidden Markov Model fits log return, realized volatility, VWAP deviation, volume z-score, and high-low ranges on the last 300 bars. It scores +2 for high confidence trending in the setup direction, +1 for mild confidence trending, or -1 for ranging/high-vol.
*   **EMA Alignment (+1):** Daily EMA12 vs EMA25 aligned with setup direction.
*   **Perfect Acceptance (+1):** All 5 of the last 5 closes are on the correct side.
*   **VWAP Distance (+1):** Price is >= 0.5% beyond the monthly VWAP.

---

## 🛠️ Monitor & Orchestration Daemons
All analytical routines run continuously via process managers or sequential pipeline triggers.

### Actionable Pullback Alerts
When the 15m Pullback gates align, the agent sends a Telegram alert with a defined **Entry Zone** (using local state tracker `accumulation_state.json` to suppress duplicates):
- **Long Entry Zone:** `[EMA 99, EMA 99 * 1.01]`
- **Short Entry Zone:** `[EMA 99 * 0.99, EMA 99]`

### Daily Regime Alerts
The regime signal script runs **once per calendar day** via `orchestrator.py`.
- **Alert Trigger:** Dispatches a Telegram notification **only** when a setup newly transitions to **HIGH** conviction, or when an active **HIGH** setup invalidates/closes.
- **DB Logging:** Logs all signals (LOW/MODERATE/HIGH/no_signal) to DuckDB in the `regime_signals` table.
