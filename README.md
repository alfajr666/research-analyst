# BTC/ETH Options & Futures Research Analyst Agent

A Python-based cryptocurrency research agent that monitors futures/perpetuals markets via CoinAnalyze and options chains via Deribit. The agent stores structured data in a local DuckDB database, runs background alert monitoring, and delivers scheduled daily briefs (and on-demand statistics) to Telegram.

---

## 📂 Project Architecture

```
options-research-analyst/
├── data/
│   └── market_data.db          # Central DuckDB database
├── logs/
│   ├── orchestrator-out.log    # Orchestrator stdout log
│   └── orchestrator-error.log  # Orchestrator stderr log
├── venv/                       # Local Python virtual environment
├── config.py                   # Environment config loader & DB schema initialization
├── ingest_coinalyze.py         # CoinAnalyze API futures data ingestion (batched, rate-limited)
├── ingest_deribit.py           # Deribit API options chain ingestion
├── backfill.py                 # One-time historical DVOL backfill script
├── accumulation_monitor.py    # Zero-API accumulation detection monitor
├── analyze.py                  # Polars & SQL queries for skew, IV Rank, profiles, and VWAP
├── brain.py                    # Tag-based market state tracking + shift detection
├── orchestrator.py             # Sequential pipeline runner (loop, once, alerts, pruning)
├── telegram_bot.py             # Telegram commands & scheduled daily brief job
├── requirements.txt            # Project python dependencies
├── ecosystem.config.js         # PM2 process manager configuration
├── symbols-for-dual-zone.md    # Tracked symbol list (63 symbols)
└── .env                        # Configuration file (ignored by Git)
```

---

## 💾 Data Ingestion

### CoinAnalyze (Futures/Perpetuals)
Fetches 15-minute OHLCV candles for all configured symbols on a continuous ingestion loop (default 15-minute interval, configured via `INGEST_INTERVAL_MINS`). Each row stores the candle's actual close timestamp from the API — not the ingestion time — so rate-limiting delays never cause timestamp drift. Snapshot data (OI, funding rate, liquidations, long/short ratio) is aligned to the same candle timestamp. Duplicate rows for the same (timestamp, symbol) pair are automatically deduplicated before insertion.

*   **Rate Limiting**: CoinAnalyze has a strict rate limit (~5 req/min sliding window). The `RateLimiter` class enforces a 12-second minimum interval between calls, exponential backoff with jitter on 429 responses, and a global penalty window that blocks all subsequent calls when a 429 is received. Symbols are fetched in batches of 15 to reduce total request count.
*   **Timestamp Auto-Detection**: The API may return candle timestamps in either epoch seconds or milliseconds. The ingestion code auto-detects the format: values > 1e12 are treated as milliseconds and divided by 1000; smaller values are treated as seconds directly.

### Deribit (Options Chains)
Fetches options instruments within 60-day expiry and ±20% of the spot price. Greeks (delta, gamma, vega, theta), mark IV, open interest, and volume are stored on every ingestion cycle (default 15 minutes) for IV Rank, skew, and term structure calculations.

---

## 📊 Analytical Indicators & Features

The system implements advanced technical analysis indicators to extract market structure directly from 15-minute futures candlestick data:

### 1. Volume & TPO (Time Price Opportunity) Profiles
*   **Point of Control (POC)**: The price level with the highest volume (Volume POC) or time spent (TPO POC).
*   **Value Area (VA)**: The price range containing 70% of the profile's volume/TPO, bounded by Value Area High (VAH) and Value Area Low (VAL).
*   **High Volume Nodes (HVNs)**: Significant peaks in volume distribution, acting as magnets or strong support/resistance zones.
*   **Low Volume Nodes (LVNs)**: Valleys in volume distribution representing price rejection zones where price moves rapidly.
*   **Timestamp Anchoring**: All profile data is anchored to the candle's actual close timestamp from the exchange API, not the ingestion time. Rate-limiting delays do not cause timestamp drift.
*   **Price Scaling Engine**: Dynamically scales sub-dollar altcoins (e.g. POPCAT, MOODENG) up to 1,000,000x for Polars list processing to prevent step errors, then scales back to actual floats.

### 2. Profile Shape Classification
Summarizes the profile's structural distribution to diagnose market sentiment:
*   **D-Shape (Balanced)**: Normal-like distribution where volume is concentrated in the middle (bracketed, rangebound market).
*   **P-Shape (Bullish Consolidation)**: Profile is thin at the bottom and thick at the top. Signals short-covering rallies or upward trend consolidation.
*   **b-Shape (Bearish Consolidation)**: Profile is thin at the top and thick at the bottom. Signals long liquidations or downward trend consolidation.
*   **B-Shape (Double Distribution)**: Two distinct high-volume areas separated by a deep middle LVN, indicating transition/breakout between value zones.

### 3. EMA & POC Confluence Entry Signal
Runs a nearness confluence comparison (with a default 0.75% threshold) to detect high-probability setups:
*   **🔥 HIGH CONFLUENCE ENTRY**: Triggered when the current Price, EMA26, EMA99, and the Volume POC are all coiled together. Setup for a breakout.
    *   **Directional Bias**: Uses EMA cross (26/99) to determine trend direction. EMA26 > EMA99 = long setup; EMA26 < EMA99 = short setup. Only the aligned direction is shown in alerts.
    *   **Confidence Scoring (5 factors)**: Each factor votes +1/-1 to produce a conviction level:
        1. Price vs VWAP alignment
        2. Price vs Value Area midpoint
        3. Profile shape alignment (P-shape/b-shape)
        4. Price vs Volume POC
        5. Volume surge confirmation
    *   **Conviction Levels**: 🔥 HIGH (≥3/5), ✅ MODERATE (1-2/5), ⚠️ LOW (≤0/5). LOW conviction alerts can be filtered via `MIN_CONVICTION` env var.
*   **⚡ STRONG CONFLUENCE**: Triggered when the price is near the POC while testing either EMA26 or EMA99.
*   **⏳ POTENTIAL ENTRY (EMA Pullback)**: Triggered when EMAs are coiled near the POC, but price has drifted. Watch for a pullback.

### 4. VWAP & 24h High/Low Range
*   **VWAP**: Calculated over the lookback window using Typical Price ($\frac{H+L+C}{3}$) weighted by volume.
*   **24h Range**: Real-time high and low prices fetched via DuckDB queries over the last 24 hours.

---

## 🔔 Background Alert System

The system runs two independent alert monitors:

### Accumulation Monitor (`accumulation_monitor.py`)
Runs as a continuous PM2 daemon that checks for volume spikes with flat price action (accumulation) every 15 minutes. It reads existing 15-min OHLCV data from the DuckDB `futures_data` table — **zero API calls** — aggregates it into 1-hour windows, and runs the same detection logic as the hourly volume/OI scanner (`VOLUME_SPIKE_THRESHOLD`, `PRICE_SILENT_THRESHOLD`). When a symbol newly enters accumulation, it sends an immediate Telegram alert. State is tracked in `data/accumulation_state.json` to prevent duplicate alerts on subsequent checks.

### High Confluence Entry Monitor (orchestrator)
The orchestrator daemon monitors all active symbols in `market_data.db` after every ingestion cycle (default 15 minutes) for alert triggers:
*   **Alert Criteria**: Fires a dedicated alert notification to Telegram when a symbol enters the `🔥 HIGH CONFLUENCE ENTRY` state.
*   **Directional Bias**: Alert shows only one side (long or short) based on EMA26 vs EMA99 cross, eliminating dual-direction clutter.
*   **Confidence Scoring**: Each alert includes a conviction level (HIGH/MODERATE/LOW) based on 5 confirming factors. LOW alerts can be filtered via `MIN_CONVICTION` env var (default: `LOW`).
*   **1h Cooldown Deduplication**: Logs alerts to the `confluence_alerts` table in DuckDB. If an alert has been dispatched for that symbol in the last 1 hour, it is suppressed to prevent notification spam.
*   **Complete Market Context**: Each alert includes the full profile picture — POC, VWAP, VAL, VAH, top HVNs/LVNs, anchored-from data range, and candle count — so you can assess the setup without running separate commands.
*   **Data Sufficiency Guard**: If an asset has less than 48 candles (12 hours of data) in its lookback window, it is flagged as `"Insufficient data"` and skipped. This prevents faulty calculations while database history is populating.

---

## 🛠️ Setup Instructions

### 1. Configure Credentials
Copy `.env.example` to `.env` and fill in the values:
```bash
cp .env.example .env
```

*   **`COINANALYZE_API_KEY`**: Register on [coinalyze.net](https://coinalyze.net) to get a free API key.
*   **`TELEGRAM_BOT_TOKEN`**: Create a Telegram bot via [@BotFather](https://t.me/BotFather) and paste the token.
*   **`TELEGRAM_CHAT_ID`**: Get the ID of the channel, group, or direct chat where you want the brief and alerts delivered.
*   **`DAILY_BRIEF_TIME_WITA`**: Time to send the daily brief (defaults to `08:00` in WITA / Asia/Makassar timezone).
*   **`MIN_CONVICTION`** (optional): Minimum conviction level for High Confluence Entry alerts (`LOW`, `MODERATE`, or `HIGH`). Set to `MODERATE` or `HIGH` to filter lower-confidence signals (defaults to `LOW`).

### 2. Install Dependencies
Initialize a virtual environment and install dependencies:
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Initialize Database
Initialize the database schemas and indexes:
```bash
python config.py
```

To wipe and reinitialize (e.g. after schema changes):
```bash
rm -f data/market_data.db && python config.py
```

### 4. Bootstrap Historical Volatility (IV Rank)
Run the one-time backfiller. This fetches the last 90 days of daily DVOL index values for BTC and ETH from Deribit to ensure the **IV Rank** calculation works accurately on day one:
```bash
python backfill.py
```

---

## 🚀 Running the Services

Use a process manager like `pm2` (configured in `ecosystem.config.js`) to run the background processes:

### Using PM2 (recommended)
```bash
pm2 start ecosystem.config.js
```

The orchestrator and accumulation-monitor both run as continuous daemons. The orchestrator runs the full ingestion + alert pipeline on a loop (default 15-minute interval, configured via `INGEST_INTERVAL_MINS`). The accumulation-monitor checks for volume-based accumulation patterns every 15 minutes (zero API calls — reads from local DuckDB). The telegram-bot runs continuously with auto-restart. All PM2 logs are rotated daily at midnight with zero retention via `pm2-logrotate`.

### After Config Changes
If you modify `ecosystem.config.js` or any orchestration files, reload PM2:
```bash
pm2 restart ecosystem.config.js
```

### Manually (or using screen)
1.  **Data Ingestion Orchestrator & Alerts Daemon** (append `--once` to run a single pipeline and exit):
    ```bash
    python orchestrator.py
    ```
2.  **Telegram Command Bot listener & Scheduler**:
    ```bash
    python telegram_bot.py
    ```

---

## 🔍 Telegram Command Reference
*   `/start` - Shows welcome message and list of commands.
*   `/brief` - Generates and sends the comprehensive BTC/ETH/SOL market brief immediately (includes 24h ranges, options, skew, and Volume Profiles with shape & TA confluence).
*   `/futures` - Focuses on perpetual metrics (price shifts, 24h Range, OI shifts, funding, liquidations).
*   `/options` - Focuses on options metrics (ATM IV, IV Rank, skew, term structure, Max Pain).
*   `/profile <symbol>` - Generates 7d Volume and TPO profiles with POC, VAH, VAL, HVN, LVN levels, profile shapes, and EMA/VWAP confluence metrics. Defaults to majors if no symbol is provided.
