# Independent LLM Thesis Review v1 — Research Analyst

**Status:** Locked design; not implemented

## 1. Decision

Research Analyst may add one optional, fail-open LLM review after deterministic
admission, scoring, and clash resolution, and immediately before shared-bus
publication. The reviewer is an independent thesis critic. It is not another
scorer, admission gate, venue adapter, execution authority, or position manager.

The reviewer is deliberately blind to the scorer's conclusion. It receives the
selected candidate's point-in-time market evidence and strategy thesis, but it
must not receive the operational quality score, scorer verdict, scorer threshold,
scorer weights, clash scores, clash rank, or publication eligibility.

The locked flow is:

```text
strategy candidates
  -> deterministic hard admission
  -> operational scorer
  -> deterministic clash resolution
  -> selected candidate
  -> independent LLM thesis review
       thesis_score >= 70 -> pass
       thesis_score <  70 -> veto
       unavailable         -> shadow/unavailable pass
  -> shared SQLite intent bus
```

The publisher remains a deterministic transport. It never invokes the model.

## 2. Scope

This specification owns:

- the review seam and ordering;
- the cutoff-bound review input;
- the model-output contract and deterministic threshold;
- failure, idempotency, and retry behavior;
- compact local review persistence and retention;
- the nested TradeIntent review metadata;
- shadow validation and promotion gates;
- downstream entry-message visibility.

It does not authorize:

- rescue of a deterministic rejection;
- modification of direction, entry, stop, target, expiry, strategy identity,
  account route, or admission proof;
- quantity, leverage, venue precision, order placement, fill claims, protection,
  or position management;
- external news retrieval or free-form web content in the live path;
- analyst-owned Discord messages for review decisions;
- storing prompts, raw model completions, or hidden reasoning.

## 3. Rollout Control

One setting owns the entire feature:

```text
LLM_THESIS_REVIEW_MODE=off|shadow|enforce
```

No legacy alias or component-specific enable flag is allowed.

| Mode | Model call | Persist review | Affect publication | Attach to passing TradeIntent |
| --- | --- | --- | --- | --- |
| `off` | no | no | no | no |
| `shadow` | yes | yes | no | yes, marked `enforced=false` |
| `enforce` | yes | yes | explicit veto suppresses | yes for pass; unavailable is shadow/unavailable and publishes |

The implementation default must be `off`. The first production observation
period must explicitly select `shadow`. Technical availability of `enforce`
does not waive the promotion gates in section 15.

The v1 threshold is a versioned policy constant:

```text
THESIS_REVIEW_PASS_THRESHOLD = 70
```

It is not a runtime tuning setting. Changing the threshold requires a new review
policy version and outcome analysis.

## 4. Module Interface

The module is deep: callers provide one immutable review input and receive one
validated review result.

```python
review_thesis(review_input, reviewer) -> ThesisReviewResult
```

The caller injects the provider adapter. The module owns evidence validation,
prompt construction, response parsing, thresholding, fallback construction,
fingerprinting, persistence shape, and metrics. The external interface must not
expose provider-specific request objects.

```text
ThesisReviewInputV1
  candidate identity + immutable geometry
  strategy thesis
  cutoff-bound evidence
  evidence provenance

ThesisReviewResultV1
  pass | veto
  thesis score or null
  one-line explanation
  review/fallback provenance
```

## 5. Ordering and Authority

The reviewer runs only for the candidate already selected by deterministic clash
resolution. It must not review every raw candidate in enforce mode and then pick
a different winner.

If the selected candidate is vetoed:

- no TradeIntent is published for that candidate;
- no suppressed clash loser is promoted;
- no second review is attempted for the symbol and cutoff;
- the deterministic scorer and clash records remain unchanged;
- the final publication state is `suppressed_by_thesis_review`.

The review result is an additional publication decision. It never overwrites a
scorer verdict. A candidate may therefore retain:

```text
scorer_verdict       = eligible
clash_decision       = selected
thesis_decision      = veto
publication_decision = suppressed
```

## 6. Review Input Contract

Every input is bound to the candidate's authoritative evaluation cutoff. No
forming bar, future observation, post-publication state, fill state, or later
market outcome may enter the review.

Required fields:

```json
{
  "schema_version": 1,
  "candidate_id": "...",
  "candidate_fingerprint": "...",
  "strategy_id": "...",
  "strategy_thesis": "...",
  "asset": "BTC",
  "direction": "long",
  "evaluation_cutoff": "...Z",
  "entry": 100.0,
  "stop": 98.0,
  "target": 104.0,
  "reward_risk": 2.0,
  "evidence": {},
  "provenance": {}
}
```

`strategy_thesis` is versioned, repository-owned text describing what must be
true for the named strategy. It is not model-generated.

The compact evidence bundle may include, when available:

- POC, HVN, LVN, VAH, VAL, normalized distance, and directional reaction;
- reaction-candle body, wick, close location, and follow-through facts;
- volume participation level and trajectory;
- OI change, price change, and their directional quadrant;
- funding level, trailing percentile, heat, and crowding direction;
- completed 1h/4h trend, regime, volatility, and structural context;
- entry extension from the relevant equilibrium or reaction area;
- availability status and source versions for every evidence family.

The evidence builder should prefer normalized facts and short bounded sequences
over raw candle dumps. Optional missing evidence is represented explicitly; it
must not be fabricated.

### 6.1 Blinding requirements

The following fields are forbidden anywhere in the review input or prompt:

- `quality_score`, `operational_score`, or total score;
- scorer verdict, score threshold, or scorer weights;
- scorer component labels such as `support` or `contradict` when the underlying
  fact can be supplied directly;
- clash competitor scores, winner margin, or rank;
- `selected_for_publication` or equivalent eligibility labels;
- expected or realized trade outcome.

The model may see facts also used by the scorer. It may not see how the scorer
weighted or concluded from them.

## 7. Model Task

The model is an adversarial, veto-only thesis reviewer. Its question is:

> Is there a concrete contradiction between the strategy thesis and the
> point-in-time evidence that makes this proposed intent unworthy of
> publication?

The rubric is:

- `0-39`: thesis clearly contradicted;
- `40-69`: material contradiction or inadequate confirmation;
- `70-84`: coherent thesis with no material contradiction;
- `85-100`: strong confirmation across independent observations.

A low score should require one fatal contradiction or at least two independent
material contradictions. Missing optional evidence alone is not a contradiction.

The score is named `thesis_score`. It is not a win probability, calibrated
confidence, sizing input, or execution authorization. Its status remains
`uncalibrated` until a separate locked outcome study proves otherwise.

## 8. Raw Model Output and Validation

The provider must return one JSON object and nothing else:

```json
{
  "schema_version": 1,
  "thesis_score": 78,
  "explanation": "Profile reaction, OI, and funding remain coherent with the long thesis."
}
```

Validation rules:

- the key set is exact;
- `schema_version` equals `1`;
- `thesis_score` is an integer from `0` through `100`;
- `explanation` is one non-empty line of at most 180 Unicode characters;
- the explanation contains no Markdown block, newline, URL, or execution claim;
- unknown keys, prose around the JSON, non-finite values, and coercion from a
  numeric string are invalid.

The model does not return `pass` or `veto`. Application code derives the binary
decision so score and decision cannot disagree:

```text
thesis_score >= 70 -> pass
thesis_score <  70 -> veto
```

Exactly `70` passes.

## 9. Failure and Availability Contract

The live pipeline is fail-open for provider availability. Provider
unavailability, timeout, rate limit, network failure, malformed output, schema
failure, serialization failure, or an open circuit produces this
application-generated binary pass. It is recorded and attached as
`mode=shadow`, `enforced=false`, and `score_status=unavailable`, even when the
configured mode is `enforce`:

```json
{
  "decision": "pass",
  "thesis_score": null,
  "score_status": "unavailable",
  "mode": "shadow",
  "enforced": false,
  "explanation": "LLM review unavailable; fail-open publication applied.",
  "reviewed": false
}
```

Do not fabricate a score of 70 for fallback results. Fail-open observations must
not enter score calibration buckets.

The v1 call has a bounded three-second deadline and no inline retry. After three
consecutive provider failures, the caller opens a 15-minute circuit breaker.
During the open interval candidates receive the same fail-open result without a
provider call. These values are versioned implementation policy, not independent
environment settings.

One candidate's review failure must never fail its evaluation cutoff, prevent
other candidates from being processed, or mark a publisher failure.

## 10. Idempotency and Retry

Review identity is derived from:

```text
candidate_fingerprint
+ evidence_hash
+ prompt_version
+ model_version
+ review_policy_version
```

The persisted review is written before publisher invocation. A publisher retry
must load and reuse the same review. It must never invoke the model again.

Changing provider configuration while a TradeIntent is retryable does not
replace the original review. A new review requires a new candidate identity or
an explicit offline research run; live code does not re-review an in-flight
candidate. No network call may occur while holding an analyst-database write
transaction.

## 11. Compact Local Persistence

The analyst database adds a compact `thesis_reviews` table. It stores the
validated result, not the prompt or raw completion.

```text
review_id                 TEXT PRIMARY KEY
candidate_id              TEXT NOT NULL
candidate_fingerprint     TEXT NOT NULL
evaluation_cutoff         TIMESTAMP NOT NULL
mode                      TEXT NOT NULL
decision                  TEXT NOT NULL        -- pass | veto
thesis_score              INTEGER NULL
score_status              TEXT NOT NULL        -- uncalibrated | unavailable
explanation               TEXT NOT NULL
reviewed                  BOOLEAN NOT NULL
fallback_reason           TEXT NULL
model_version             TEXT NULL
prompt_version            TEXT NOT NULL
review_policy_version     TEXT NOT NULL
evidence_hash             TEXT NOT NULL
latency_ms                INTEGER NULL
created_at                TIMESTAMP NOT NULL
```

Constraints enforce the two decision values, score range, null score for
`reviewed=false`, and explanation length.

Local persistence is required because enforce-mode vetoes never become
TradeIntents, publisher retries need stable decisions, and shadow validation
must join reviews to later counterfactual candidate outcomes.

### 11.1 Retention

- retain compact review rows for 120 days;
- run bounded deletion at most once per day through the database-owning writer;
- delete at most 1,000 expired rows per transaction and repeat on later passes;
- never run `VACUUM` or equivalent compaction in the live review path;
- use the repository's existing offline compaction owner to reclaim file space;
- never delete a review still referenced by an active or retryable outbox item.

## 12. TradeIntent Metadata Contract

No new top-level TradeIntent schema version is required. A passing or fail-open
review is carried in the existing metadata object under one versioned key:

```json
{
  "metadata": {
    "thesis_review": {
      "schema_version": 1,
      "review_id": "...",
                        "mode": "shadow",
                        "enforced": false,
      "decision": "pass",
      "thesis_score": 78,
      "score_status": "uncalibrated",
      "explanation": "Profile reaction, OI, and funding remain coherent with the long thesis.",
      "reviewed": true,
      "model_version": "...",
      "prompt_version": "thesis-review-v1",
      "review_policy_version": "thesis-review-policy-v1",
      "evidence_hash": "..."
    }
  }
}
```

The bus publisher validates that:

- enforce-mode intents contain `decision=pass`;
- `review_id` resolves to the persisted review for the outer candidate ID;
- the metadata candidate identity matches the reviewed candidate;
- reviewed scores and derived decisions agree;
- fallback passes have `thesis_score=null`, `reviewed=false`, and
  `score_status=unavailable`;
- review metadata is JSON-safe and contains no sizing field.

Shadow-mode TradeIntents may carry a counterfactual `decision=veto`, but must set
`mode=shadow` and `enforced=false`. Consumers must never treat shadow metadata as
an execution block. The shared bus stores and transports the metadata but does
not interpret the review or call a model.

## 13. Discord and Executor Visibility

Research Analyst sends no Discord notification for pass, veto, fallback, shadow,
or provider-health decisions.

When an executor confirms an actual venue entry, its existing entry message may
render the attached review:

```text
LLM thesis review: 78 PASS — Profile reaction, OI, and funding remain coherent with the long thesis.
```

Shadow metadata must be visibly labeled `SHADOW`. Fail-open metadata must show
`UNAVAILABLE / FAIL-OPEN PASS`, not a fabricated numeric score.

If no order is filled, no entry message is emitted. Vetoes therefore create no
Discord message. Entry-message ownership and venue-confirmed wording remain
executor responsibilities; this repository never claims execution. Discord is
not the review ledger and Discord delivery failure cannot change any review or
publication decision.

## 14. Observability

Health and structured logs expose bounded aggregates, not prompts or evidence:

- configured mode, prompt version, model version, and policy version;
- attempted, reviewed, pass, veto, and fail-open counts;
- provider error and open-circuit counts by reason category;
- p50/p95 latency and deadline count;
- score histogram in the four rubric bands;
- review persistence and reuse counts;
- publisher intents containing review metadata.

No API key, endpoint credential, full prompt, raw completion, or hidden reasoning
may enter logs, databases, Discord, or TradeIntent metadata.

## 15. Shadow Evaluation and Promotion

Remain in shadow for at least eight complete weeks and at least 300 reviewed,
resolved candidates before considering enforcement. More data is required when
strategy-level or regime-level slices are sparse.

Evaluation uses point-in-time candidate outcomes, not executor PnL, because vetoed
candidates have no fill. The locked analysis reports:

- outcome and normalized-R distribution by thesis-score band;
- pass and veto counts by strategy, direction, and regime;
- winners vetoed and losers vetoed;
- counterfactual expectancy with and without the veto;
- opportunity loss from vetoed winners;
- score monotonicity and clustering;
- reviewed coverage, fail-open rate, latency, and circuit-open time;
- stability across chronological out-of-sample windows;
- results separately for every model and prompt version.

Promotion requires all of the following:

1. `<70` candidates are materially worse than `>=70` candidates on locked,
   chronological holdout data.
2. Counterfactual expectancy improves after vetoes without an unacceptable loss
   of valid opportunities.
3. The relationship is not driven by one strategy, direction, asset, or regime.
4. Review availability and latency meet the live-path budget.
5. Score bands remain stable for the exact locked model and prompt versions.
6. A replay proves scorer blinding and no lookahead.
7. Bus and executor compatibility tests preserve the nested metadata.

If evidence is insufficient, keep shadow. If an enforced model or prompt changes,
the new version returns to shadow independently.

## 16. Required Tests

### Interface and blinding

1. Input construction rejects scorer totals, verdicts, weights, thresholds,
   clash scores, ranks, and publication labels.
2. Every evidence timestamp is cutoff-bound.
3. Evidence order and hashing are deterministic.
4. Strategy thesis version participates in evidence provenance.

### Output and threshold

5. Scores `0`, `69`, `70`, and `100` derive veto, veto, pass, and pass.
6. The model cannot return a conflicting decision field.
7. Multiline, oversized, non-JSON, extra-key, and out-of-range outputs fail open.
8. A valid explanation remains exactly one line in persistence and metadata.

### Modes and failures

9. Off mode makes no provider call and preserves current publication behavior.
10. Shadow veto persists but cannot suppress publication.
11. Enforce veto produces no TradeIntent and does not promote a clash loser.
12. Timeout, rate limit, provider error, parse error, and open circuit produce a
    binary fail-open pass with a null score and shadow/unavailable metadata.
13. One review failure cannot fail the cutoff or another candidate.

### Persistence and delivery

14. Publisher retries reuse the persisted review without another provider call.
15. No provider call occurs in the publisher or within a write transaction.
16. Passing and fail-open intents carry valid `metadata.thesis_review` on every
    enabled target.
17. Enforce-mode handoff rejects inconsistent score/decision metadata.
18. Review metadata never changes geometry, routing, sizing, or admission proof.
19. Veto rows survive long enough for outcome analysis.
20. Bounded retention preserves active/retryable references and deletes only rows
    older than 120 days.

### Notification ownership

21. The analyst sends no review Discord message.
22. Executor fixtures can render pass, shadow, and fail-open metadata only on a
    venue-confirmed entry message.

## 17. Acceptance Criteria

Implementation is complete only when:

- one review mode controls all behavior with no aliases;
- the reviewer is demonstrably blind to scorer and clash conclusions;
- application code, not the model, derives pass/veto at the locked threshold;
- unavailable review is a recorded shadow/unavailable pass and never interrupts
  the pipeline;
- the publisher remains model-free and retry-idempotent;
- compact local records are retained and cleaned under the locked policy;
- passing review provenance crosses the shared bus as nested metadata;
- vetoes remain locally auditable but create neither TradeIntent nor Discord
  message;
- only the executor may show the review on a venue-confirmed entry message;
- shadow outcome evidence satisfies section 15 before enforcement.
