# Quality Score Outcome Research

**As of:** 2026-09-13T23:46:27Z UTC

## Conclusion

- The analyst-side `alpha_outcomes` table contains **0 rows**, but the first
  pass was incomplete: venue journals provide a separate, authoritative
  execution-outcome path. These are venue-confirmed outcomes, not the missing
  descriptive candidate-outcome ledger.
- The Bybit venue sample does **not support** the thesis. It has 21 closed
  Research Analyst positions: 3 wins and 18 losses, for a 14.29% win rate and
  -434.46 total realized PnL. At `>=0.50`, 18 closed positions produced 2 wins
  and 16 losses, an 11.11% win rate and -399.46 PnL. The score/PnL Pearson
  correlation is `0.046757`; score versus binary win is `-0.100753`.
- The Propr venue sample is incomplete but also does not establish the thesis.
  Of 48 closed Research Analyst positions with PnL, only 11 still join to a
  scored shared-bus event: 2 wins and 9 losses, 18.18% win rate, and -1,343.96
  PnL. Within those 11, `>=0.50` has 2/8 wins (25.00%) and -1,136.21 PnL;
  `>=0.60` has 2/6 wins (33.33%) and -600.88 PnL; `>=0.70` has 0/3 wins and
  -954.04 PnL. Score/PnL correlation is `-0.077449`; score versus binary win
  is `0.307117`. These are too few observations for a reliable conclusion.
- Raising the threshold to `>=0.50` is materially more selective in the scored
  sample: 3,795/6,040 signals (62.83%) remain, versus 5,938/6,040 (98.31%) at
  the default `>=0.30`. It removes 2,143 of the default-eligible signals
  (35.9%). Selectivity alone does not establish better outcomes.
- Higher cutoffs produce the following descriptive retention rates:

| Threshold | Retained | Retention | Excluded | 95% Wilson CI for retention |
| ---: | ---: | ---: | ---: | ---: |
| >=0.30 | 5,938/6,040 | 98.31% | 102 | 97.92%-98.58% |
| >=0.50 | 3,795/6,040 | 62.83% | 2,245 | 61.57%-64.01% |
| >=0.60 | 2,719/6,040 | 45.02% | 3,321 | 43.73%-46.24% |
| >=0.70 | 1,187/6,040 | 19.65% | 4,853 | 18.64%-20.64% |

The intervals quantify uncertainty in the observed retention fraction only.
They are not outcome confidence intervals and do not correct for live-feed
selection, strategy mix, repeated assets, or time dependence.

## Authoritative Definitions

- `specs/trade-quality-scoring-and-regime-profiles-v1.md:52-66` defines
  `quality_score` as a bounded execution-quality multiplier, **not a
  probability**. `0.30` is the default analyst execution threshold and `0.50`
  means 50% of venue-configured base risk, not a 50% win-probability claim.
- `src/research_analyst/trade_quality.py:96-107` selects the operational score
  and applies `TRADE_QUALITY_MIN_SCORE`; the score is copied to both
  `quality_score` and `score`.
- `src/research_analyst/strategy_plugins.py:918-928` persists that score with
  its score-policy version in `raw_signal_status_history`. This analysis uses
  only `score_policy_version='trade-quality-v2'`, excluding legacy admission and
  v1 scores, and deduplicates repeated status rows by `raw_signal_id`. Repeated
  v2 rows for each raw signal had the same score.
- The active delivery path is `src/research_analyst/alpha_outbox.py:180-234`
  to `src/research_analyst/intent_bus_publisher.py:62-105`, then the shared
  SQLite bus. `src/research_analyst/execution_adapter.py` defines a legacy
  filesystem adapter, but no current source caller invokes
  `ExecutionAdapter.deliver()`.
- Bybit consumes `target=bybit` in
  `/home/ubuntu/bybit-executor/executor/intent_bus_consumer.py:63-180` and
  reaches the CCXT venue adapter in
  `/home/ubuntu/bybit-executor/executor/exchange.py:22-36`. Propr consumes
  `target=propr` in `/home/ubuntu/propr-executor/src/propr_executor/intent_bus_consumer.py:156-215`.
- `specs/alpha-signal-contract-and-confidence-calibration-v1.md:249-280`
  defines the intended descriptive outcomes: `win` is target-before-
  invalidation, `loss` is invalidation-before-target, `timeout` is neither
  barrier within the horizon, and `unfilled` is a limit entry not reached.
  Ambiguous same-candle barriers use invalidation-first. Fill-conditional
  success and end-to-end success must remain separate.
- `specs/regime-session-module-v1.md:616-618` explicitly says candidate-level
  outcome data must be populated before score approval and that candidate market
  outcomes must remain separate from realized executor PnL.

## Data Coverage

The read-only snapshot contained 17,642 v2 score status rows representing
6,040 distinct raw signals, observed from `2026-09-10T03:10:00Z` through
`2026-09-13T23:45:59Z`. Scores ranged from `0.232010` to `0.892683`, with mean
`0.561174` and sample standard deviation `0.139079` (the score-status table is
live, so counts can advance between queries).

The database has a legacy-shaped `alpha_outcomes` table, but it is empty and
the current repository has no outcome writer/evaluator reference for that
table. Current v2 `raw_signals.candidate_id` values also have zero joins to
`alpha_candidates.candidate_id`, the foreign-key path used by `alpha_outcomes`.
It is therefore not the live execution outcome source.

The venue outcome sources are:

- Bybit: `/home/ubuntu/bybit-executor/data/executor.db`, joining
  `position_closures.position_id` to the matching profile's
  `intents.delivery_id`; the score is in `intents.original_json`.
- Propr: `/home/ubuntu/propr-executor/data/db/propr_positions.db`, joining the
  `delivery_id` in `positions.entry_reason` to the shared bus event; the score
  is in `bus_events.source_json`.

The Propr bus retains terminal events for a limited period and the Propr
journal does not persist `quality_score`, which is why 37 of its 48 closed
Research Analyst positions cannot currently be score-linked. Bybit's 21
closures all have venue-derived realized PnL, but all are classified as
`UNKNOWN_VENUE_CLOSE`, so exit-reason attribution is weak even though the PnL
itself is present.

The evidence does not confirm a monotonic score-to-outcome relationship. The
Bybit result is directly contrary to the proposed threshold effect; Propr's
small surviving sample is mixed. A future calibration analysis should preserve
score at venue-journal open time and segment at least by venue/account,
strategy, direction, and setup/channel rather than pool unlike populations.

## Reproducibility

All database reads used SQLite read-only mode and a single transaction per
measurement; no settings, databases, or production processes were changed.

Outcome availability and join check:

```bash
sqlite3 -readonly data/analyst.sqlite3 "
BEGIN;
SELECT 'alpha_outcomes' AS metric, COUNT(*) AS n FROM alpha_outcomes
UNION ALL SELECT 'non_null_net_return', COUNT(*) FROM alpha_outcomes WHERE net_return IS NOT NULL
UNION ALL SELECT 'wins', COUNT(*) FROM alpha_outcomes WHERE outcome = 'win'
UNION ALL SELECT 'losses', COUNT(*) FROM alpha_outcomes WHERE outcome = 'loss'
UNION ALL SELECT 'timeouts', COUNT(*) FROM alpha_outcomes WHERE outcome = 'timeout'
UNION ALL SELECT 'unfilled', COUNT(*) FROM alpha_outcomes WHERE outcome = 'unfilled';

WITH scored AS (
  SELECT s.raw_signal_id, s.score,
         ROW_NUMBER() OVER (
           PARTITION BY s.raw_signal_id
           ORDER BY s.recorded_at DESC, s.status_id DESC
         ) AS rn
  FROM raw_signal_status_history AS s
  WHERE s.score_policy_version = 'trade-quality-v2'
    AND s.score IS NOT NULL
), one AS (
  SELECT raw_signal_id, score FROM scored WHERE rn = 1
)
SELECT COUNT(*) AS scored_raw_signals,
       SUM(score >= 0.30) AS ge_030,
       SUM(score >= 0.50) AS ge_050,
       SUM(score >= 0.60) AS ge_060,
       SUM(score >= 0.70) AS ge_070
FROM one;

WITH scored AS (
  SELECT s.raw_signal_id, s.score,
         ROW_NUMBER() OVER (
           PARTITION BY s.raw_signal_id
           ORDER BY s.recorded_at DESC, s.status_id DESC
         ) AS rn
  FROM raw_signal_status_history AS s
  WHERE s.score_policy_version = 'trade-quality-v2'
    AND s.score IS NOT NULL
), one AS (
  SELECT raw_signal_id, score FROM scored WHERE rn = 1
)
SELECT COUNT(*) AS score_outcome_joins
FROM one
JOIN raw_signals AS r USING (raw_signal_id)
JOIN alpha_outcomes AS o ON o.candidate_id = r.candidate_id;
ROLLBACK;
"
```

Venue outcome join and threshold analysis:

```bash
sqlite3 -readonly /home/ubuntu/bybit-executor/data/executor.db "
WITH joined AS (
  SELECT c.realized_pnl,
         json_extract(i.original_json, '$.quality_score') AS score
  FROM position_closures AS c
  JOIN intents AS i
    ON i.exchange_id = c.exchange_id
   AND i.account_id = c.account_id
   AND i.delivery_id = c.position_id
  WHERE json_extract(i.original_json, '$.source') = 'research-analyst'
    AND c.realized_pnl IS NOT NULL
)
SELECT CASE WHEN score >= 0.70 THEN '>=0.70'
            WHEN score >= 0.60 THEN '>=0.60'
            WHEN score >= 0.50 THEN '>=0.50'
            ELSE '<0.50' END AS threshold_band,
       COUNT(*) AS n,
       SUM(realized_pnl > 0) AS wins,
       SUM(realized_pnl) AS pnl
FROM joined
GROUP BY threshold_band;
"
```

The equivalent Propr join extracts `positions.entry_reason.delivery_id`, joins
it to `bus_deliveries.delivery_id` and `bus_events.event_id`, and extracts the
score from `bus_events.source_json`. Propr's venue journal supplies
`realized_pnl` directly from its settled close-fill path.

The retention intervals above use the 95% Wilson interval with `z=1.96` and
`n=6,040`. Venue correlations and threshold summaries are descriptive only;
the Bybit sample has 21 closures and the score-linked Propr sample has 11.
Neither sample supports production threshold promotion or a causal claim.
