# BTC/ETH Options & Futures Research Analyst Agent

A Python-based cryptocurrency research agent that monitors futures/perpetuals markets via CoinAnalyze and options chains via Deribit. The agent stores structured data in a local DuckDB database, runs background alert monitoring, and delivers scheduled daily briefs (and on-demand statistics) to Telegram.

---

## 📂 Project Architecture

```
options-research-analyst/
├── data/
│   └── market_data.db          # Central DuckDB database
├── venv/                       # Local Python virtual environment
├── config.py                   # Environment config loader & DB schema initialization
├── ingest_coinalyze.py         # CoinAnalyze API futures data ingestion (supports batched altcoin fetching)
├── ingest_deribit.py           # Deribit API options chain ingestion
├── backfill.py                 # One-time historical DVOL backfill script
├── analyze.py                  # Polars & SQL queries for skew, IV Rank, profiles, and VWAP
├── orchestrator.py             # Sequential pipeline runner (runs loop, once, and alerts)
├── telegram_bot.py             # Telegram commands & scheduled daily brief job
├── requirements.txt            # Project python dependencies
└── .env                        # Configuration file (ignored by Git)
```

---

## 📊 Analytical Indicators & Features

The system implements advanced technical analysis indicators to extract market structure directly from 15-minute futures candlestick data:

### 1. Volume & TPO (Time Price Opportunity) Profiles
*   **Point of Control (POC)**: The price level with the highest volume (Volume POC) or time spent (TPO POC).
*   **Value Area (VA)**: The price range containing 70% of the profile's volume/TPO, bounded by Value Area High (VAH) and Value Area Low (VAL).
*   **High Volume Nodes (HVNs)**: Significant peaks in volume distribution, acting as magnets or strong support/resistance zones.
*   **Low Volume Nodes (LVNs)**: Valleys in volume distribution representing price rejection zones where price moves rapidly.
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
*   **⚡ STRONG CONFLUENCE**: Triggered when the price is near the POC while testing either EMA26 or EMA99.
*   **⏳ POTENTIAL ENTRY (EMA Pullback)**: Triggered when EMAs are coiled near the POC, but price has drifted. Watch for a pullback.

### 4. VWAP & 24h High/Low Range
*   **VWAP**: Calculated over the lookback window using Typical Price ($\frac{H+L+C}{3}$) weighted by volume.
*   **24h Range**: Real-time high and low prices fetched via DuckDB queries over the last 24 hours.

---

## 🔔 Background Alert System

The orchestrator daemon monitors all active symbols in `market_data.db` after every 15-minute ingestion cycle for alert triggers:
*   **Alert Criteria**: Fires a dedicated alert notification to Telegram when a symbol enters the `🔥 HIGH CONFLUENCE ENTRY` state.
*   **1h Cooldown Deduplication**: Logs alerts to the `confluence_alerts` table in DuckDB. If an alert has been dispatched for that symbol in the last 1 hour, it is suppressed to prevent notification spam.
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

### 4. Bootstrap Historical Volatility (IV Rank)
Run the one-time backfiller. This fetches the last 90 days of daily DVOL index values for BTC and ETH from Deribit to ensure the **IV Rank** calculation works accurately on day one:
```bash
python backfill.py
```

---

## 🚀 Running the Services

Use a process manager like `pm2` (configured in `ecosystem.config.js`) to run the background processes:

### In Loop Mode (using PM2)
```bash
pm2 start ecosystem.config.js
```

### Manually (or using screen)
1.  **Data Ingestion Orchestrator & Alerts Daemon**:
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
