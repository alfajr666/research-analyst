# Admission-Owned Structural SL Admission v4: Optional 15m Zones

## Status

Superseded by `specs/structural-sl-admission-v5-nearest-zone.md`. This document
records the former priority-only implementation. It extends
`specs/structural-sl-admission-v3.md` with an independently toggled 15m zone
fallback. It does not authorize implementation of the retired historical 15m
strategy design, and it does not change the existing 4h/1h behavior while the
toggle is off.

The v4 contract becomes authoritative for an evaluation only when
`STRUCTURAL_15M_ZONES_ENABLED=true` is loaded by that evaluator process. The
default is `false`.

## 1. Decision

Every execution candidate continues to require generic admission and
admission-owned structural SL admission before scoring or delivery.

The strategy still supplies the entry, direction, proposed
`invalidation_price`, and target. Admission still selects a zone and validates
the proposed stop without mutating it.

When 15m zones are enabled, the selector checks the strongest eligible
structural timeframe in this order:

```text
4h direct regime history
  -> 1h direct regime history
    -> 15m market 5m history resampled in memory
```

When 15m zones are disabled, the selector and proof must be behaviorally
identical to v3:

```text
4h direct regime history
  -> 1h direct regime history
```

15m is a lower structural timeframe than 1h and 4h. It is not an alternative
strategy evaluation interval, not a regime-session input, and not a replacement
for direct 1h/4h strategy setup data.

## 2. Goals and Non-Goals

### Goals

- Add one global, easy-to-toggle admission setting for 15m zones.
- Reuse the tested FVG, order-block, ATR, cutoff, and stop-buffer rules.
- Keep 4h and 1h direct Bybit REST history regime-owned and read-only.
- Use committed market-owned completed 5m bars as the only 15m source.
- Preserve source-blind strategy plugins and admission-owned structural context.
- Make the selected 15m zone and its source bars fully auditable.
- Allow a passing 15m proof through alpha outbox and shared-bus verification.
- Make disabling 15m a true no-load, no-selection, no-proof-change path.

### Non-goals

- Do not revive the historical 15m EMA99 strategy, candle-color strategy, or
  15m trigger design.
- Do not expose structural zones through strategy snapshots.
- Do not use 15m zones to score candidates or activate regime families.
- Do not replace direct 1h/4h strategy frames with canonical resampled frames.
- Do not write 15m structural zones into `regime.sqlite3` or
  `structure_zones`.
- Do not use `LSR_V1_USE_15M_EPHEMERAL_FVG` as this toggle.
- Do not mutate a strategy's proposed stop to make it pass.
- Do not make 15m a hard requirement when the toggle is disabled.

## 3. Current-System Audit

The implementation must preserve these existing ownership rules:

| Concern | Current owner and behavior | v4 requirement |
| --- | --- | --- |
| 4h/1h strategy setup | Regime-owned direct Bybit REST cache | Unchanged |
| 4h/1h structural zones | Admission-owned context from regime DB | Unchanged |
| 15m market bars | Market DB, canonical 5m observations plus derived 15m rows | Read canonical 5m rows and resample in memory |
| Zone detector | `structure_zones.detect_fvg` and `detect_order_blocks` | Reuse with explicit 15m timeframe |
| ATR | In-house Wilder ATR(14) | Reuse; selected zone timeframe determines structural ATR |
| Selector | Newest eligible zone, priority 4h then 1h | Add 15m after 1h only when enabled |
| Stop authority | Proposed strategy stop remains authoritative | Unchanged |
| Delivery proof | Recomputed and compared by `intent_outbox` | Add 15m and source provenance verification |
| Strategy awareness | Plugins receive no structural zone records | Unchanged |
| Structural persistence | Zones recomputed in memory | Unchanged |

The current implementation has two important constraints that v4 must address:

1. `structural_stop.build_structural_contexts()` currently opens only the
   regime database and loads only 4h/1h frames.
2. `intent_outbox.verify_intent_admission()` currently accepts only `1h` and
   `4h` as selected structural timeframes.

Both are deliberate seams for this change, not reasons to add a second writer,
a new worker, or a strategy-specific bypass.

## 4. Configuration and Toggle Contract

Add these settings to `src/research_analyst/config.py`:

```text
STRUCTURAL_15M_ZONES_ENABLED=false
STRUCTURAL_15M_LOOKBACK_DAYS=16
STRUCTURAL_15M_READINESS_BARS=57
```

### 4.1 Enable flag

`STRUCTURAL_15M_ZONES_ENABLED` is a process-start configuration flag.

- `false` is the safe and compatibility default.
- `true` enables 15m loading and 15m selection only for the current evaluator
  process.
- Any value accepted by existing boolean settings (`1`, `true`, `yes`, `on`)
  means enabled.
- The flag is not read from candidate data, strategy metadata, or a database
  row.
- It must not change in the middle of a cutoff. A managed process restart is
  required for a rollout change.
- The flag is independent of `EVAL_INTERVALS`.
- The flag is independent of `LSR_V1_USE_15M_EPHEMERAL_FVG`.

The effective structural contract version is:

```text
STRUCTURAL_15M_ZONES_ENABLED=false -> structural-sl-admission-v3
STRUCTURAL_15M_ZONES_ENABLED=true  -> structural-sl-admission-v4-15m
```

The selected proof must record the effective contract version. A v4 proof must
not be produced while the flag is disabled.

### 4.2 Lookback and readiness

`STRUCTURAL_15M_LOOKBACK_DAYS` is the lookback passed to the canonical loader
before zone detection. The default is 16 days, which preserves the loader's
current minimum and provides substantially more than the readiness window. The
implementation may expose a bar-count setting instead, but it must convert that
setting to the loader's day-based interface and must not request less than the
configured readiness window.

`STRUCTURAL_15M_READINESS_BARS` is the minimum number of complete, contiguous,
cutoff-bound 15m bars required before 15m zone detection is considered ready.
The default of 57 provides a separate, explicit lower-timeframe readiness
contract and is not inherited from the 1h/4h ADX requirement.

The implementation must validate that readiness is sufficient for the detector:

```text
readiness >= max(3, ATR_PERIOD, ORDER_BLOCK_SWING_LOOKBACK + 2)
```

The implementation must not fabricate bars, lower the configured readiness at
runtime, or treat an incomplete 15m bucket as a closed bar.

## 5. Data Ownership and Loading

### 5.1 15m source

The 15m structural frame must be built from the market-owned `5m` observations
using the existing canonical loader and resampler:

```text
market.sqlite3 source_observations, interval=5m
  -> cutoff-bound duplicate/source preference
  -> complete UTC 15m buckets
  -> 15m FVG/OB and Wilder ATR(14)
```

Use the existing `strategy_v2_context.load_bars_for_interval(..., "15m", ...)`
path or extract its implementation behind a small internal module interface.
Do not independently query or reinterpret the persisted derived 15m rows for
the structural path. The on-demand path preserves the base 5m observation IDs
needed for audit and replay.

The structural builder may open `market.sqlite3` read-only in addition to its
existing read-only `regime.sqlite3` connection. It must never write either
database.

### 5.2 4h and 1h source

4h and 1h structural frames remain loaded only from regime-owned direct history:

```text
regime.sqlite3 direct Bybit REST 1h/4h bars
  -> source_end <= evaluation_cutoff
  -> readiness and contiguous-window validation
  -> FVG/OB and Wilder ATR(14)
```

There is no canonical 5m fallback for 1h or 4h. Enabling 15m must not alter
that rule.

### 5.3 Source mixing

15m bars use the canonical source-preference rules already used for execution
frames. Each contributing 5m row must be:

- committed before the evaluation begins;
- complete and finite;
- cutoff-bound by `source_end <= evaluation_cutoff`;
- selected through the existing duplicate and boundary-alias rules;
- traceable to a non-empty `source_observation_id`.

The frame may contain the accepted canonical Bybit REST and Bybit WS source
mix used by the market pipeline. Mixed source is not silently labeled
`pure_ws`; the proof records the source mode and source provenance explicitly.
An invalid row, conflicting duplicate, gap, or unknown source must make the
15m frame unavailable for that asset and cutoff.

## 6. Cutoff and Bar Boundary Contract

The evaluation cutoff remains the only authoritative cutoff.

For a 5m evaluation cutoff of `07:20`, the latest eligible 15m bar is the
complete bar ending at `07:15`. A forming bucket ending after the cutoff is
never eligible.

The 15m loader must:

- accept only source bars with `source_end <= evaluation_cutoff`;
- normalize exact-boundary and boundary-minus-one-millisecond representations;
- reject conflicting duplicate aliases;
- require exactly three 5m bars per 15m bucket;
- reject internal gaps and malformed OHLCV values;
- exclude the incomplete current bucket;
- return bars sorted by logical 15m end timestamp;
- preserve all contributing 5m source observation IDs.

Zone creation, confirmation, mitigation, fill, and invalidation timestamps must
all be no later than the evaluation cutoff. No future 5m bar may affect the
state of a 15m zone.

## 7. Zone Detection Contract

15m zones use the same in-house implementations and parameters as 1h/4h:

```text
FVG:          structure_zones.detect_fvg(..., tf="15m")
Order blocks: structure_zones.detect_order_blocks(..., tf="15m")
ATR:          in-house Wilder ATR(14)
```

The detector must receive only the finalized 15m frame. It must not inspect the
candidate's strategy snapshot, account, route, or regime decision.

Each normalized 15m zone must include:

- canonical asset;
- `timeframe="15m"`;
- `type` of `fvg` or `order_block`;
- directional `bullish` or `bearish` value;
- finite `low` and `high` bounds;
- active or partial lifecycle state;
- creation and confirmation timestamps;
- source evidence IDs for the contributing 5m bars;
- deterministic zone identity;
- `coverage_status="covered"`;
- detector and resampling provenance.

No 15m zone is persisted as a first-class row in `structure_zones`. The
existing table remains advisory and is not a structural admission source.

## 8. Selector Precedence

When enabled, `select_structural_zone()` checks:

1. Eligible 4h zones.
2. Eligible 1h zones when no eligible 4h zone exists.
3. Eligible 15m zones when no eligible 4h or 1h zone exists.

Within one timeframe, the newest eligible zone wins using the existing
deterministic `(created_at, zone_id)` ordering.

The selector must not choose a lower timeframe merely because its proposed-stop
geometry would pass. If an eligible 4h zone is selected and the proposed stop
fails against it, admission fails. It must not retry 1h or 15m. The same rule
applies to an eligible 1h zone versus 15m.

If 15m is disabled, the selector must not load, detect, select, or mention a
15m zone in the result.

An eligible zone retains the existing requirements:

- asset matched;
- requested direction matched;
- declared timeframe matched;
- active or partial;
- covered by valid source evidence;
- non-stale and not forming, filled, invalidated, or superseded;
- created and confirmed no later than the exact cutoff;
- entry on the valid directional side of the zone.

## 9. Entry and Stop Geometry

The v3 directional containment rules remain unchanged for every timeframe.

For a bullish support zone:

```text
long entry >= zone.low
```

For a bearish resistance zone:

```text
short entry <= zone.high
```

Contained entries are allowed and record zero entry buffer. Outside entries
must remain between `0.5` and `3.0` selected-zone ATR from the outer boundary.

The proposed strategy stop remains authoritative. For the selected zone:

```text
long:
    stop_buffer = zone.low - proposed_stop
    0.5 * selected_zone_ATR <= stop_buffer <= 3.0 * selected_zone_ATR

short:
    stop_buffer = proposed_stop - zone.high
    0.5 * selected_zone_ATR <= stop_buffer <= 3.0 * selected_zone_ATR
```

Therefore, a long stop must be below the selected support zone low, and a short
stop must be above the selected resistance zone high. Being merely outside the
zone is not enough; the ATR buffer must also be within bounds.

The generic admission floor remains separate and unchanged:

```text
abs(entry - proposed_stop) / entry
    >= max(INTENT_MIN_STOP_DISTANCE_PCT, 0.25 * 4h_ATR14 / entry)
```

Selecting a 15m zone does not replace the generic 4h ATR14 requirement. A
candidate can pass 15m structural geometry and still fail generic admission if
the 4h ATR14 is unavailable or the entry-to-stop distance is too small.

## 10. Structural Context Interface

Extend the internal structural-context builder with one dependency while
keeping callers small:

```python
build_structural_contexts(
    candidates,
    cutoff,
    *,
    regime_db_path=None,
    market_db_path=None,
)
```

The builder owns:

- deciding whether 15m is enabled;
- opening read-only database connections;
- loading the correct source for each timeframe;
- validating readiness and coverage;
- computing ATR and source IDs;
- detecting and normalizing zones;
- attaching immutable context provenance.

The caller (`strategy_plugins`) only passes `config.MARKET_DB_PATH` and the
existing regime path. No strategy plugin receives this context.

The context must distinguish source and readiness per timeframe:

```json
{
  "asset": "ACU",
  "cutoff": "2026-09-08T07:20:00Z",
  "zones": [],
  "atr_by_timeframe": {
    "4h": 0.0123,
    "1h": 0.0045,
    "15m": 0.0018
  },
  "coverage_status": {
    "4h": "covered",
    "1h": "covered",
    "15m": "covered"
  },
  "timeframe_provenance": {
    "4h": {"source_mode": "bybit_rest_direct"},
    "1h": {"source_mode": "bybit_rest_direct"},
    "15m": {"source_mode": "market_5m_resampled"}
  }
}
```

When disabled, `coverage_status`, `atr_by_timeframe`, and provenance must not
contain a 15m entry. This prevents an off-mode proof from accidentally changing
because 15m data happens to be present.

## 11. Admission Proof and Handoff

The selected admission result must include the existing v3 fields plus:

```json
{
  "structural_admission_contract_version": "structural-sl-admission-v4-15m",
  "structural_15m_zones_enabled": true,
  "selected_zone_timeframe": "15m",
  "structural_source_mode": "market_5m_resampled",
  "structural_source_exchange": "bybit",
  "structural_resampling_contract_version": "execution-5m-to-15m-v1",
  "structural_zone_detector_version": "structure-zones-v1",
  "structural_frame_bar_ids": [],
  "structural_atr_source_bar_ids": [],
  "selected_zone_source_evidence_ids": []
}
```

`structural_frame_bar_ids` for 15m are deterministic IDs for the complete
15m frame inputs or the complete ordered base-5m evidence set, as defined by
the implementation. The proof must make it possible to reconstruct exactly
which bars were used; a single derived-row ID without base evidence is not
enough.

The proof must preserve the existing fields:

- selected zone ID, type, asset, timeframe, and state;
- zone bounds and creation/confirmation timestamps;
- entry location and entry buffer in price and ATR units;
- stop buffer in price and ATR units;
- selected structural ATR, period, method, and source IDs;
- exact structural context cutoff;
- unchanged candidate entry, stop, target, direction, and identity fingerprint.

`intent_outbox.verify_intent_admission()` must:

- accept `15m` as a selected timeframe only for a v4 proof;
- reject a v4 proof claiming `15m` while the effective toggle is off;
- recompute the 15m frame and selected zone from the cutoff-bound market data;
- verify the resampling, detector, source mode, frame IDs, zone evidence, ATR
  source IDs, geometry, and exact cutoff;
- reject a proof whose selected timeframe or contract version is changed;
- continue accepting valid v3 proofs in the disabled compatibility path only;
- reject a v3 proof that claims a 15m selected timeframe.

The alpha outbox and shared-bus publisher must carry the proof unchanged. No
direct intent write may bypass proof verification.

## 12. Failure Semantics

15m behavior is fail closed but toggle-scoped:

| Condition | Toggle off | Toggle on |
| --- | --- | --- |
| No 15m data | Ignore 15m; use v3 4h/1h path | Try 4h, then 1h; use 15m only if ready |
| Incomplete/gapped 15m frame | Ignore 15m | 15m unavailable; fail only if no eligible 4h/1h zone exists |
| Invalid 15m zone | Ignore invalid zone | Exclude it; select another eligible 15m zone or fail |
| Selected 4h stop geometry fails | v3 fail | Fail; never fall back to 1h/15m |
| Selected 1h stop geometry fails | v3 fail | Fail; never fall back to 15m |
| Selected 15m stop geometry fails | Not applicable | Fail; do not mutate stop or retry another timeframe |
| 4h ATR unavailable for generic admission | Fail as today | Fail as today |
| Market DB unavailable | No effect | 15m unavailable; preserve 4h/1h fallback |
| Proof source/provenance mismatch | v3 validation | Reject proof |

An unavailable 15m frame must not turn an otherwise valid 4h/1h candidate into
a failure. Once a valid stronger-timeframe zone is selected, lower-timeframe
availability is irrelevant.

## 13. Observability

Each pipeline evaluation must report:

- `structural_15m_zones_enabled`;
- structural contract version;
- number of candidate assets for which 15m was loaded;
- 15m readiness count and unavailable count;
- selected structural timeframe counts (`4h`, `1h`, `15m`, none);
- 15m structural rejection counts by reason;
- 15m source mode and resampling contract version;
- exact evaluation cutoff and effective feed ID.

Candidate admission results must distinguish these reasons:

```text
15m structural context unavailable
15m structural frame incomplete or gapped
no eligible 15m HTF structural zone
15m structural stop buffer is below minimum ATR multiple
15m structural stop buffer is above maximum ATR multiple
```

Logs must not say that an intent was sent, accepted, filled, or executed when
the candidate stopped at structural admission.

## 14. Wiring Plan

Implement in this order:

1. Add and validate the three configuration settings. Keep the default off.
2. Add explicit 15m source and resampling provenance constants.
3. Extend `build_structural_contexts()` with a read-only market DB adapter and
   15m loading only when enabled.
4. Extend zone normalization and selector precedence to support `15m`.
5. Keep 4h/1h direct loading and precedence behavior unchanged.
6. Extend admission result fields and bump the effective structural contract
   only for the enabled path.
7. Pass the effective context through `strategy_plugins` to the selected
   candidate and `alpha_outbox`.
8. Extend `intent_outbox` proof recomputation and selected-timeframe validation.
9. Add observability fields without changing executor semantics.
10. Update `specs/structural-sl-admission-v3.md` with a cross-reference stating
    that v4 is the optional 15m extension, while retaining v3 as the disabled
    behavior contract.
11. Update `README.md`, `docs/DESIGN.md`, `agent.md`, and the relevant strategy
    and ingestion references. Mark historical 15m strategy documents as
    historical where they are not this admission feature.

No new worker, database writer, schema migration, exchange credential, or
executor change is required.

## 15. Test Plan

### 15.1 Toggle compatibility

- Default configuration is off.
- Off mode produces byte-equivalent v3 selection/proof fields for a fixed 4h/1h
  fixture.
- Off mode does not open the market DB for structural contexts.
- Off mode never emits `15m` in selector results, provenance, or proofs.
- On mode loads 15m only for assets that emitted candidates.
- Changing the setting requires a managed process restart and cannot affect a
  cutoff already in progress.
- The 15m setting does not alter `EVAL_INTERVALS` or the LSR toggle.

### 15.2 15m loader and cutoff

- Three complete 5m bars create one 15m bar at the correct UTC boundary.
- A forming 15m bucket is excluded.
- Exact-boundary and boundary-minus-one-millisecond aliases normalize once.
- Conflicting aliases fail closed.
- Missing internal 5m bars fail the 15m frame.
- Malformed OHLCV and invalid volume fail the 15m frame.
- Bars after the evaluation cutoff cannot appear in the frame.
- Source preference is deterministic and base 5m IDs are preserved.
- REST/WS source provenance is recorded without incorrectly claiming pure WS.
- Market DB is read-only and regime DB remains read-only.

### 15.3 Zone detector and selector

- FVG and order-block detectors receive finalized 15m frames only.
- 4h beats 1h and 15m.
- 1h beats 15m when no eligible 4h exists.
- 15m is selected only when no eligible 4h/1h exists and the toggle is on.
- Newest eligible zone wins within one timeframe.
- A stronger selected zone failing geometry does not fall back to a weaker zone.
- Opposing, stale, forming, future, filled, invalidated, uncovered, malformed,
  and wrong-side zones are excluded.
- No selected zone is written to `structure_zones`.

### 15.4 Geometry and admission

- Long 15m support entry inside the zone passes with a stop at least 0.5 ATR
  below `zone.low`.
- Short 15m resistance entry inside the zone passes with a stop at least 0.5
  ATR above `zone.high`.
- Boundary values at 0.5 and 3.0 ATR are inclusive.
- Stops inside the zone, on the wrong side of the boundary, too close, or too
  far fail closed.
- Outside-entry proximity remains 0.5-3.0 selected-zone ATR.
- Proposed entry, stop, and target are never mutated.
- Generic 4h ATR14 admission remains independently enforced.
- Missing 15m data falls back to eligible 4h/1h zones when enabled.

### 15.5 Handoff and replay

- A valid selected-15m v4 proof reaches the alpha outbox and shared bus.
- v3 proofs remain valid only in the disabled path.
- A 15m proof is rejected when the toggle is off.
- Mutated timeframe, contract version, cutoff, zone bounds, zone evidence, ATR,
  source mode, frame IDs, or stop buffer is rejected.
- Replaying with changed 5m source data cannot pass an old proof.
- A candidate rejected by 15m structural admission creates no alpha event and
  no executor intent.

### 15.6 Regression

- Existing direct 1h/4h tests remain green and never read canonical 5m fallback.
- Existing strategy tests remain source-blind and receive no zone records.
- Existing LSR ephemeral 15m tests remain unchanged.
- Existing Fundamo/Hyro symbol-account policy remains independent of zone
  timeframe.
- Full suite, compileall, and cutoff replay checks pass with the toggle both off
  and on.

## 16. Rollout and Operations

Rollout is staged:

1. Implement with `STRUCTURAL_15M_ZONES_ENABLED=false`.
2. Run unit, integration, replay, lookahead, and proof-verification tests.
3. Deploy with the toggle off and verify v3-equivalent behavior.
4. Enable 15m in shadow diagnostics only if an explicit shadow output is added;
   shadow must not change hard admission or delivery.
5. Enable hard 15m fallback through the managed orchestrator process after
   reviewing selected-timeframe and rejection counts.
6. If the toggle changes, restart through `oxmgr`; do not start a duplicate
   orchestrator manually.
7. Verify fresh cutoff logs, structural timeframe counts, proof validation,
   pipeline completion, publisher state, and executor handoff state.

Because the setting is imported through shared configuration, a deployment that
changes the checked-in configuration module or common environment should restart
the affected managed analyst processes together: symbol rotation, WebSocket,
regime session, and orchestrator. Enabling the feature itself changes admission
behavior in the orchestrator; no executor restart is required.

## 17. Acceptance Criteria

- With the toggle off, production behavior and v3 proofs remain unchanged.
- With the toggle on, selector precedence is exactly `4h > 1h > 15m`.
- 15m uses only cutoff-bound market-owned 5m data resampled in memory.
- 1h/4h continue using only regime-owned direct history.
- An eligible stronger zone is never bypassed because a weaker zone would pass.
- Structural stop geometry remains 0.5-3.0 ATR beyond the selected zone boundary.
- Generic 4h ATR admission remains in force.
- Candidate strategy snapshots remain free of structural zone records.
- Every selected 15m zone has reproducible source, resampling, detector, ATR,
  cutoff, and evidence provenance.
- Final intent verification accepts valid v4 proofs and rejects mutated proofs.
- The 15m toggle is one explicit setting, defaults off, and is operationally
  safe to change through a managed restart.
- No new database writer, worker, executor credential, or order-placement path
  is introduced.
