# Option-Scanner LLM Selector Feasibility

**Assessment date:** 2026-09-02
**Scope:** adopting the option-scanner selector's design and behavior in
`research-analyst`, including WebSocket and local resource consumption.

## Executive Assessment

Adoption is **feasible with a redesign at the integration boundary**. The
option-scanner selector has several good reusable behaviors:

- two-stage market-state then strategy-family selection;
- deterministic whitelist/policy filtering before model selection;
- complete structured score validation, score clamping, and short reasons;
- shadow mode, explicit authority modes, and fail-precise fallback;
- audit fields linking a decision to the context and selected strategies.

It is not drop-in compatible with this analyst because its runtime model is
different:

- option-scanner uses a wall-clock loop and in-memory activation coordination
  (`option-scanner/agent/llm_selector.py:356-383`);
- this analyst consumes durable completed-5m triggers and processes them in
  cutoff order (`src/research_analyst/orchestrator.py:694-725`);
- option-scanner's context is BTC/ETH options-implied data plus chart features
  (`option-scanner/agent/llm_selector.py:502-521`), while this analyst evaluates
  Bybit perpetual strategies over a rotated symbol universe;
- this analyst's hard admission and executor routing must remain authoritative
  (`src/research_analyst/strategy_plugins.py:574-596`,
  `src/research_analyst/intent_outbox.py:62-146`).

The safe target is therefore a **cutoff-scoped selector that filters plugin
evaluation for one cutoff**. It must not mutate global strategy state, bypass
admission, or create another WebSocket/database writer.

## Source Design

### What option-scanner does well

The selector first asks the model to classify market state, applies a
deterministic family policy, and then asks the model to score only permitted
families (`option-scanner/agent/llm_selector.py:401-452`). The selector is
restricted to configured IDs and its score validator rejects unmanaged IDs,
missing reasons, non-finite values, duplicates that cannot be normalized, and
incomplete score sets (`option-scanner/agent/llm_selector.py:173-238`).

The model output is converted to a sorted top-K set with a minimum score
threshold (`option-scanner/agent/llm_selector.py:241-252`). The selector has
shadow and authoritative modes, and failure falls back to the rule-based state
instead of leaving an unknown activation set
(`option-scanner/agent/llm_selector.py:473-500`). Its plan explicitly requires
whitelisting, no sizing/parameter authority, cooldowns, shadow rollout, and
full audit persistence (`option-scanner/IMPLEMENTATION_PLAN_LLM_SELECTOR.md:101-133`).

These behaviors are good candidates for reuse.

### What must not be copied

The selector's loop evaluates on `interval_seconds=300` but enforces a default
900-second cooldown (`option-scanner/agent/llm_selector.py:283-300,
356-363`). Its state, including the last selection timestamp and active IDs, is
held in process memory (`option-scanner/agent/llm_selector.py:302-323`). That is
acceptable for a continuously running scanner with an in-process bus, but not
for a replayable analyst pipeline.

Option-scanner publishes `StrategyActivationProposal` on an in-process
`asyncio` bus (`option-scanner/agent/llm_selector.py:454-467`; strategy tasks
consume activation updates in `option-scanner/scanner/runner.py:107-139`). A
restart can therefore lose transient selector state. In this repository the
cross-process contracts are the filesystem trigger spool, analyst SQLite
state, alpha ledger, and shared intent bus; substituting an in-memory bus would
break replay and handoff guarantees (`src/research_analyst/evaluation_trigger.py:38-118`,
`specs/event-driven-5m-evaluation.md:123-151`).

The options-specific context also cannot be reused. Option-scanner's regime
snapshot makes exchange calls for options chains, daily/hourly OHLCV, funding,
and risk-free rate for BTC and ETH (`option-scanner/agent/regime_controller.py:15-98`).
The analyst should build context from the immutable market observations and
materialized features already associated with the requested cutoff, not fetch
"now" data from a second venue/API path.

## Proposed Analyst Shape

```text
completed 5m trigger
        |
        v
load/finalize cutoff snapshot
        |
        +--> deterministic eligible strategy set
        |       (registered + enabled + operator-active)
        |
        +--> selector, only on selector cadence
        |       market state -> deterministic policy -> family/strategy scores
        |       persist decision, input hash, raw response, validation, fallback
        |
        v
reuse last valid selection between selector cutoffs
        |
plugin evaluation filter for this cutoff only
        |
existing hard admission -> clash resolution -> alpha event -> intent bus
```

The insertion seam is between feature materialization and plugin invocation in
`src/research_analyst/orchestrator.py:417-501`. The resulting selected IDs
should be passed into `invoke_plugins_for_intervals` and applied as an
**ephemeral per-cutoff filter** near
`src/research_analyst/strategy_plugins.py:478-539`. Do not use
`set_plugin_state`/`plugin_states` for this: that table is a persistent
operator/runtime toggle (`src/research_analyst/strategy_plugins.py:153-169,
225-245`), whereas a selector result is a decision for one immutable cutoff.

The selector must never replace the following path:

1. candidate hard gates and freshness checks;
2. `resolve(candidates)` clash/ranking policy;
3. `write_event` and intent geometry validation;
4. per-strategy account routing and executor-owned sizing;
5. shared SQLite intent-bus publication.

The analyst already keeps those responsibilities in the plugin and outbox
paths (`src/research_analyst/strategy_plugins.py:574-596`,
`src/research_analyst/intent_outbox.py:149-199`, and
`src/research_analyst/intent_bus_publisher.py:46-84`).

## Behavior Compatibility

| Concern | Option-scanner | Analyst-compatible adoption |
|---|---|---|
| Decision unit | Current wall-clock context | Completed 5m `cutoff_id` and immutable snapshot |
| Selection target | Logical option families, expanded to variants | Registered perpetual strategy IDs or an explicitly defined family catalog |
| Context | BTC/ETH option-implied plus chart/enrichment data | Bybit market observations, 1m/5m bars, resampled 15m/1h/4h, zones, and strategy features |
| Cadence | Poll every 5m, select at most every 15m | Consume every 5m trigger; recompute selection every 15m by default and reuse it in intervening cutoffs |
| Failure | Keep last state or rule fallback | Keep last persisted valid selection, otherwise deterministic allowlist/rule set; never block admission indefinitely |
| Authority | Activation proposal through in-process bus | Per-cutoff evaluation filter only; operator states remain separate |
| Audit | DuckDB/SQLite selector and unified LLM logs | New analyst-owned selector decision table keyed by cutoff and input hash |
| Execution | Selector cannot order or size | Preserve admission, routing, sizing, and executor ownership unchanged |

The analyst's current live health snapshot shows 11 strategies, 32 symbols
evaluated, and 64 strategy evaluations in the last cycle
(`data/health.json:12-53`). A global top-3 family decision copied literally
from option-scanner could suppress valid strategy/symbol opportunities across
that universe. The first implementation should select strategy IDs for the
cutoff, with family grouping added only if the strategy catalog explicitly
defines equivalent families.

## WebSocket Impact

### No additional WebSocket should be added

The existing gateway is already the sole market database writer and streams
1m/5m Bybit klines; higher timeframes are locally resampled
(`src/research_analyst/ws_gateway.py:1-14`). Its stream planner shards symbols
by connection and its provider task is independent of orchestrator evaluation
(`src/research_analyst/ws_gateway.py:240-265,
681-767`). The selector should read committed observations/features. It should
not subscribe to exchange topics, poll a second feed, or share a connection
pool with the gateway.

### Measured baseline on 2026-09-02

| Component | Observation |
|---|---:|
| `research-analyst-ws` | 153 MB RSS, 0.5% CPU in `oxmgr status`; healthy |
| `research-analyst-orchestrator` | 136 MB RSS, 42.1% instantaneous CPU in `oxmgr status`; likely cycle-dependent |
| `research-analyst-pm-sidecar` | 122 MB RSS, 0.0% CPU in `oxmgr status`; healthy |
| Gateway health | 34 subscribed symbols, 12 reported active connections, zero reconnects |
| Analyst DB | 663 MB `data/analyst.sqlite3` |
| Market DB | 550 MB `data/market.sqlite3` |
| Option-scanner process | No online `option-scanner` process in `pm2 list`; no live selector RSS/CPU baseline |
| Option-scanner data | 10 GB under `/home/ubuntu/option-scanner/data` |

The gateway health file reports 12 active connections
(`data/ws_health.json:4-11`), while a same-time `ss -Htanp` snapshot showed
four established outbound sockets owned by the gateway PID. This discrepancy
means the health counter should not be treated as a precise capacity metric
without further instrumentation. The provider increments the counter on
connect but does not visibly decrement it on a clean cancellation during feed
reconciliation (`src/research_analyst/ws_gateway.py:688-724,
798-801`). This is an existing observability issue, not a reason to add
selector WebSockets.

### Expected WS cost of adoption

If the selector reads the analyst database, expected incremental WS cost is

- no new exchange connection;
- no new subscription topics;
- no new gateway queue messages;
- no change to the single-writer market DB rule.

The selector will add read queries against `market.sqlite3`. Those should use
one read-only connection, cutoff-bounded queries, and bounded feature payloads.
The read cost is likely small compared with the existing 32-symbol plugin
evaluation, but it must be measured under a slow LLM response to confirm that
the gateway continues ingesting normally. The event-driven evaluation spec
explicitly requires that websocket ingestion continue while evaluation is
deliberately slowed (`specs/event-driven-5m-evaluation.md:316-325`).

## LLM, CPU, and Storage Budget

### Request volume

The option-scanner selector makes two sequential logical completions per
selection: market-state classification and family scoring
(`option-scanner/agent/llm_selector.py:401-434`). With a 900-second cooldown:

```text
4 selector evaluations/hour
× 2 logical completions/evaluation
= 8 successful provider calls/hour baseline
```

Its validation client tries tool, JSON-object, and plain modes
(`option-scanner/agent/llm_client.py:46-51`), can retry with a JSON-only nudge,
and can walk a primary/fallback model ladder
(`option-scanner/agent/llm_client.py:31-39,
54-127`). With two models and both message variants, the theoretical retry
surface is up to 12 provider attempts per logical completion, or up to **96
attempts/hour** in a pathological invalid/error case. Timeouts are 90 seconds
for the selector (`option-scanner/agent/llm_selector.py:47-54`), so this is a
real latency/backlog risk if copied inline.

For this analyst:

- a 15-minute selector cadence gives the same 8 logical calls/hour for the
  two-stage design;
- a one-stage score-only design gives 4 logical calls/hour;
- evaluating the selector on every 5m trigger would raise the two-stage rate to
  24 logical calls/hour and is not recommended;
- existing PM calls are separate: approximately one call per managed open
  position per 5m cycle, with one retry allowed
  (`src/research_analyst/pm_sidecar.py:366-397`,
  `src/research_analyst/config.py:445-450`).

The selector's model calls must therefore have an independent bounded timeout,
retry budget, and queue policy. A failed selector must immediately reuse the
last valid selection or deterministic baseline; it must not hold a 5m trigger
until the full worst-case retry surface is exhausted.

### Local CPU and memory

For the current 11-strategy/32-symbol snapshot, selector-side CPU and memory
should be modest because it performs one small context build and one/two HTTP
requests per selection, not one request per symbol or strategy. The dominant
local costs remain feature materialization, Polars/indicator work, and the
existing plugin scans. The current 42.1% orchestrator CPU reading is a point-in-
time sample and must not be used as a steady-state capacity number.

The new audit row should store hashes and bounded JSON rather than unbounded
duplicate market history. A practical retention policy is 30-90 days for
selector decisions, with raw responses truncated or compressed after the
operational audit window. At four decisions/hour this is 96 rows/day before
retry/error detail, which is negligible relative to the current 663 MB analyst
database but still requires a retention policy.

### Latency and backlog

The current orchestrator claims one trigger and runs the pipeline sequentially
(`src/research_analyst/orchestrator.py:697-725`). An inline 20-90 second selector
call would not block the WebSocket process, but it would delay trigger
consumption. During a restart or provider outage, triggers must be replayed in
ascending order and stale candidates must not become executable
(`specs/event-driven-5m-evaluation.md:216-225`).

Recommended latency policy:

1. run the selector only at a configured selector cutoff, not every 5m cutoff;
2. use the prior persisted selection for ordinary cutoffs;
3. impose a short selector-specific timeout, initially 10-20 seconds;
4. on timeout/error/invalid output, persist the failure and use the prior or
   deterministic selection immediately;
5. expose selector latency, fallback count, and pending-trigger depth in health;
6. never run selector work in the WebSocket gateway task.

## Risks and Required Controls

| Risk | Severity | Control |
|---|---|---|
| Global activation suppresses unrelated symbols/strategies | High | Per-cutoff plugin filter; never mutate `plugin_states` |
| Selector uses future/"now" data | High | Build and hash context at the requested cutoff; query `source_end <= cutoff` |
| LLM latency creates trigger backlog | High | 15m cadence, bounded timeout/retries, previous-selection fallback |
| Invalid model output changes all strategy state | High | Exact whitelist, complete score set, finite values, deterministic fallback |
| Restart loses selection state | High | Persist decision and effective IDs in analyst DB; deterministic decision ID |
| Selector bypasses execution safeguards | High | Keep `resolve`, geometry, freshness, routing, and intent-bus gates unchanged |
| Duplicate selector decision on replay | Medium | Key by `cutoff_id` plus input hash; idempotent insert |
| Prompt/audit data grows analyst DB | Medium | Bound context/raw response and retain selector rows by TTL |
| Misread WS capacity | Medium | Fix connection-counter accounting and measure actual sockets/messages |
| Existing triple/extra interval evaluation multiplies CPU | Medium | Ensure selector does not trigger 1m/15m work; follow 5m-only target |

The current event-driven spec already identifies that unrelated interval
evaluation causes CPU/database contention and duplicate candidate churn
(`specs/event-driven-5m-evaluation.md:227-239`). Selector
adoption should not proceed by adding another evaluation owner on top of that
behavior.

## Rollout Recommendation

### Phase 0: prepare the seam

- Define an analyst strategy/family catalog with IDs, cadence, required data,
  and operator allowlist.
- Add a selector decision table containing `decision_id`, `cutoff_id`,
  `input_hash`, model, prompt version, raw response, validation result,
  selected IDs, effective IDs, fallback reason, latency, and created time.
- Add an ephemeral `selected_strategy_ids` argument to plugin invocation.
- Add selector health fields and retention/pruning.

### Phase 1: shadow mode

- Run at most once per 15 minutes, attached to a completed cutoff.
- Make no change to plugin activation or intent delivery.
- Compare selector selection with the deterministic baseline and record
  candidate/emission differences and realized outcomes.
- Test invalid JSON, unmanaged IDs, missing IDs, timeout, provider outage,
  restart after persistence, and replay of old cutoffs.

### Phase 2: guarded influence

- Permit the selector to filter only the registered per-cutoff strategy set.
- Keep operator-inactive strategies inactive regardless of model output.
- Fall back to the deterministic baseline on any selector uncertainty.
- Require an agreed shadow gate before authoritative influence. Option-scanner's
  own plan uses a 1-2 week shadow period, about 80% agreement with the rule
  controller, and no worse realized P&L on disagreements
  (`option-scanner/IMPLEMENTATION_PLAN_LLM_SELECTOR.md:119-133`).

### Phase 3: optimize after measurement

- Tune context size and one-stage versus two-stage calls using observed token,
  latency, fallback, and outcome data.
- Fix the gateway connection health counter before relying on connection-budget
  alerts.
- Consider an isolated selector worker only if inline latency remains a
  trigger-backlog problem. It must communicate through analyst-owned durable
  state, not a second market DB or a second WebSocket gateway.

## Decision

Proceed with a **shadow-mode, cutoff-scoped adaptation**. Reuse the
option-scanner selector's validation, deterministic policy, shadow/fallback
behavior, and audit ideas. Do not copy its wall-clock loop, in-memory bus,
options-specific context, or global activation semantics.

The adoption is resource-feasible if it adds no WebSocket subscriptions and
limits selection to roughly four decisions/hour. It is not production-safe if
the LLM is placed directly in every 5m evaluation, if failures can leave an
empty active set, or if model output mutates persistent plugin state.

## Evidence Index

- Option-scanner selector: `option-scanner/agent/llm_selector.py`
- Option-scanner LLM retry/mode behavior: `option-scanner/agent/llm_client.py`
- Option-scanner policy and rollout: `option-scanner/IMPLEMENTATION_PLAN_LLM_SELECTOR.md`
- Option-scanner context source: `option-scanner/agent/regime_controller.py`
- Option-scanner task activation: `option-scanner/scanner/runner.py`
- Analyst trigger consumer: `src/research_analyst/orchestrator.py`
- Analyst plugin/admission seam: `src/research_analyst/strategy_plugins.py`
- Analyst market-data ownership: `src/research_analyst/ws_gateway.py`
- Analyst trigger contract: `specs/event-driven-5m-evaluation.md`
- Analyst PM boundary: `specs/llm-position-sidecar.md`
- Runtime measurements: `data/health.json`, `data/ws_health.json`, `oxmgr status`, `pm2 list`, `du`, and `ss -Htanp` captured on 2026-09-02
