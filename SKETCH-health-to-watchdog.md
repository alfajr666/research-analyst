# Sketch: Wiring research-analyst health to bot-health-watchdog

## Why different?
This is **not** a trading bot (no positions, trades, rug checks, P&L).
Trading bots (gem-hunter, bybit, mexc, propr) report:
- openPositions, totalTradesExecuted, evalsLastCycle, finalSignalsLastCycle, rejections, etc.

For research-analyst (orchestrator):
- Focus on **data platform health** + **research pipeline health**.
- Core pain point we just fixed: data freshness (source_observations 15m max source_end, age).
- External data sources: CoinAnalyze (CA) + OpenMarket (OM) request health (429/ok counts).
- Pipeline execution: inserted rows per cycle, cutoff processing, LLM usage (new).
- DB / lock health, rate limiter status.

## Proposed health.json structure (non-trading variant)
```json
{
  "bot": "research-analyst",
  "cycle": 1234,
  "cycleIntervalMs": 900000,
  "lastCycleAt": "2026-08-18T05:00:00Z",
  "pipelineMode": "full",
  "halted": false,
  "haltReason": null,
  "dataFreshness": {
    "max15mSourceEnd": "2026-08-18T05:00:00Z",
    "ageMin": 11.7,
    "barsLast30m": 10,
    "coreSymbolsFresh": ["BTCUSDT_PERP.A", "ETHUSDT_PERP.A", "SOLUSDT_PERP.A"],
    "lastInserted": 4
  },
  "ca": {
    "429": 51,
    "ok": 14,
    "error": 1,
    "lastCycle": {"429": 9, "ok": 5}
  },
  "om": {
    "ok": 4,
    "rate_limited_pre": 2,
    "lastCycle": {"ok": 4}
  },
  "llm": {
    "callsLastCycle": 0,
    "tokensIn": 0,
    "tokensOut": 0,
    "costUsd": 0,
    "enabled": false
  },
  "cutoffs": {
    "lastCutoffId": "cutoff-2026-08-18T05-00-00Z",
    "zonesComputed": 38
  },
  "db": {
    "locksLastCycle": 2,
    "lastLockPid": "3061492"
  },
  "ts": "2026-08-18T05:11:00Z",
  "startedAt": "..."
}
```

## How to wire
1. In orchestrator.py:
   - At end of each _run_pipeline (or on schedule), collect metrics from existing prints (Health line, inserted count, cutoff, request_log queries).
   - Write to `data/health.json` (create writer similar to gem's health-writer.js but Python).
   - Use atomic write (tmp + rename).

2. Add target to bot-health-watchdog/src/index.js TARGETS:
   {
     id: 'research-analyst',
     label: 'research-analyst',
     healthPath: '/home/ubuntu/research-analyst/data/health.json',
     pm2Names: ['orchestrator'],
     defaultIntervalMs: 900_000,
     isTradingBot: false
   }

3. Update watchdog checks:
   - If !isTradingBot: skip trade DB, openPositions.
   - Core checks still apply: file exists + mtime fresh (based on lastCycleAt + cycleIntervalMs * STALE_MULT).
   - Add custom alerts e.g. if dataFreshness.ageMin > 30, or ca.429 > 80% in last cycle.
   - Use same Telegram/Discord send functions.

4. LLM integration (using provided key):
   - Provider: OpenRouter (sk-or- prefix).
   - Model: "chagpt-luna" (or exact "chatgpt-luna" / "luna" – confirm in code).
   - Set in orchestrator .env or config:
     LLM_API_KEY=sk-or-...
     LLM_MODEL=chagpt-luna
     LLM_PROVIDER=openrouter  # may need small adapter if current code assumes openai
   - Optional: use LLM in health writer to generate a short "healthSummary" field for alerts (e.g. "Freshness good, 3 CA 429s on ohlcv but core ok").

5. PM2 / ecosystem:
   - Ensure data/ dir exists.
   - Add to research-analyst ecosystem if separate.
   - Watchdog already monitors pm2 'orchestrator'.

6. Migration / backcompat:
   - Keep printing the "Health: ..." line in orchestrator logs for humans.
   - health.json for machine (watchdog).

## Sketch for health writer (Python)
Similar to JS:
- state dict with the fields above.
- def write_health(): atomic json dump to data/health.json
- Call at end of pipeline, after collecting:
  - from existing conn queries for source_observations max, request_log counts.
  - from cycle counters.

## Risks / notes
- DB locks can still affect health write (use try/finally, separate conn?).
- Keep health write lightweight (no heavy queries in writer).
- Since non-trading, watchdog may need small if/else for alerts (e.g. don't alert on "no trades" ).
- Test with manual write + watchdog cycle.

## Status
- Implemented:
  - orchestrator.py now writes data/health.json (dataFreshness + ca/om stats) after each cycle.
  - bot-health-watchdog/src/index.js has research-analyst target (non-trading).
  - .env + .env.example updated with LLM_ keys + RESEARCH_ paths.
  - Verified: health.json generated with current freshness (age ~7m, source_end advancing).
- LLM key set locally; features remain disabled until explicitly used.
- Sketch complete; health contract matches non-trading focus (freshness first).

Next steps (future): enhance health.json with barsLast30m/llm/cutoffs if needed; add age-based alerts in watchdog for research-analyst.
