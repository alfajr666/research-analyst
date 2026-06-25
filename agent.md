# Options Research Analyst Agent Spec

This document details the configuration and operational spec for the **Options Research Analyst Agent**.

## 🤖 Agent Purpose
The agent operates as an analytical system monitoring crypto futures (via CoinAnalyze) and options markets (via Deribit). Its primary background function is detecting high-probability structural trend setups and delivering actionable entry signals directly to Telegram.

## 📈 Confluence Trading Strategy
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

## 🛠️ Monitor Daemon (`accumulation_monitor.py`)
The monitor runs continuously as a PM2 daemon, executing every 15 minutes.

### Actionable Alerts
When all three gates align, the agent sends a Telegram alert with a defined **Entry Zone**:
- **Long Entry Zone:** `[EMA 99, EMA 99 * 1.01]`
- **Short Entry Zone:** `[EMA 99 * 0.99, EMA 99]`

### Spam Prevention
To ensure clean notifications, the agent uses a local state tracker (`accumulation_state.json`). It will only dispatch a single alert when a coin enters the setup range, suppressing duplicates until the coin leaves the pullback zone or accumulation cycle.
