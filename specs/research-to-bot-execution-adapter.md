# Research-To-Bot Execution Adapter

## Status

Proposed implementation specification. This document defines the execution
handoff from `research-analyst` to these existing bots:

- `bybit-binance-trading-agent`
- `bybit-binance-trading-agent-test`
- `mexc-trading-agent`
- `propr-trading-agent`

## Goal

Deliver a validated research alpha event to a bot as a pre-evaluated local
signal. The bot keeps ownership of account state, sizing, exchange precision,
position limits, risk limits, order placement, fill reconciliation, and stop
loss maintenance.

Research owns the final immutable trade thesis: asset, direction, entry, stop
loss, take profit, entry expiry, confidence, and strategy tag. The adapter and
receiving bot validate and forward those emitted event fields without
reinterpreting them.

```text
research alpha event
       |
       v
research-owned adapter and durable target outbox
       |
       v
minimal bot inbox reader
       |
       v
existing bot ranked execution and order lifecycle
```

The adapter must not parse Telegram messages. It consumes the authoritative
version-1 alpha JSON event persisted by `signal_publisher.py`.

## Non-Goals

- Do not expose exchange credentials to `research-analyst`.
- Do not let research calculate quantity, leverage, or exchange precision.
- Do not invoke the bots' local strategy evaluators for a research event.
- Do not make research write directly to bot SQLite databases.
- Do not change the handling of locally generated bot signals.
- Do not infer a market entry when the research event specifies a limit entry.

## Terminology

| Term | Meaning |
| --- | --- |
| Alpha event | Immutable, venue-neutral research event. |
| Target | One named bot delivery destination: `bybit`, `bybit-test`, `mexc`, or `propr`. |
| Delivery | One alpha event normalized for one target. |
| Inbox item | The target-specific JSON file written by research and consumed by one bot. |
| Research strategy ID | Bot-facing tag `RS-{alpha.strategy_id}`, for example `RS-accumulation-base-v1`. |
| Entry expiry | Time after which a resting entry limit must be cancelled. It never closes a filled position. |

## Preconditions

Only an event satisfying all conditions below is eligible for execution
delivery:

1. `schema_version == 1`.
2. Event status is `active`.
3. Current UTC time is before `valid_until`.
4. `entry_condition.type == "limit_at_ema_context"` for the first rollout.
5. `entry_condition.price`, `invalidation_price`, and exactly one target are
   finite positive numbers.
6. Directional geometry is valid.

```text
LONG:   stop_loss < entry_price < take_profit
SHORT:  take_profit < entry_price < stop_loss
```

Unsupported entry-condition types are skipped, not translated to a market
order. Multi-target events are skipped until a target-allocation policy is
explicitly added.

## Target Capability Rules

The research adapter applies static target eligibility. The receiving bot
always applies authoritative live-venue eligibility immediately before order
placement.

| Target | Research-side static rule | Bot-side authoritative rule |
| --- | --- | --- |
| `bybit` | Optional configured base-asset allowlist | Symbol exists in loaded CCXT Bybit USDT-linear swap markets. |
| `bybit-test` | Same as Bybit | Symbol exists in that test instance's loaded CCXT Bybit/Binance USDT-linear swap markets. |
| `mexc` | Optional configured base-asset allowlist | Symbol exists in loaded CCXT MEXC perpetual swap markets. |
| `propr` | Base is present in Propr's tradeable-assets snapshot | `is_tradeable(base)` is true, then canonicalize with `to_propr()`. |

No target is inferred from another target's listing. For example, a Bybit
listing does not establish that MEXC or Propr can trade the asset.

An unsupported symbol is terminally skipped for that target with the delivery
reason `unsupported_symbol`; it is not retried until a later, distinct alpha
event is produced.

## Research-Owned Adapter

### Location

Add an execution adapter module to `research-analyst` with these concerns:

```text
execution_adapter.py
  - select active alpha events
  - validate executable geometry
  - map each event to configured targets
  - write atomic inbox items
  - persist delivery state and reasons

data/execution_outbox/
  bybit/
  bybit-test/
  mexc/
  propr/
```

The module runs after alpha persistence. It may be called by the existing
publisher loop, but its failure must not block Telegram delivery or alpha
persistence.

### Delivery State

Persist delivery state in research's analyst SQLite database. A delivery is unique by
`(alpha_id, target)`.

| Field | Meaning |
| --- | --- |
| `alpha_id` | Immutable research event identity. |
| `target` | Bot target name. |
| `status` | `pending`, `written`, `acknowledged`, `skipped`, or `failed`. |
| `reason` | Machine-readable reason for a terminal skip/failure. |
| `inbox_path` | Absolute or repository-relative target inbox item path. |
| `written_at` | Timestamp after the atomic rename completes. |
| `acknowledged_at` | Timestamp recorded when the bot writes its receipt. |
| `bot_trade_id` | Target bot trade identifier, if a limit was accepted. |
| `bot_order_id` | Exchange order identifier, if available. |

The adapter must never rewrite a `written` or `acknowledged` delivery. This
prevents a publisher retry from creating a second execution request.

### Atomic Write

For each target, the adapter:

1. Writes JSON to a same-directory temporary filename.
2. Flushes and fsyncs the file.
3. Renames it to `<alpha_id>.json` atomically.
4. Records `written` only after the rename succeeds.

The bot may only read `*.json` files, never temporary files.

## Inbox Contract

This is the only cross-repository file contract. It is versioned and must be
strictly validated by both producer and consumer.

```json
{
  "schema_version": 1,
  "target": "bybit",
  "delivery_id": "<alpha_id>:bybit",
  "alpha_id": "<immutable-alpha-id>",
  "source": "research_analyst",
  "strategy_id": "RS-accumulation-base-v1",
  "entry_tag": "RS-accumulation-base-v1",
  "asset": "VIRTUAL",
  "symbol": "VIRTUAL/USDT:USDT",
  "direction": "SHORT",
  "order_type": "limit",
  "entry_price": 0.5558,
  "stop_loss": 0.564903,
  "take_profit": 0.542146,
  "take_profit_mode": "fixed_full_close",
  "observed_at": "2026-08-17T06:00:00Z",
  "entry_valid_until": "2026-08-17T10:00:00Z",
  "confidence": "<confidence from alpha event>",
  "research_event": {
    "strategy_id": "accumulation-base-v1",
    "setup_class": "accumulation_base",
    "phase": "confirmed_pullback"
  }
}
```

All timestamps are RFC 3339 UTC timestamps. `delivery_id` is deterministic.
The `confidence` placeholder represents the required numeric value copied from
the immutable alpha event. The research event's feature snapshot must not be
copied into bot trade metadata unless a specific diagnostic use requires it.

## Bot Inbox Consumer

Each target receives one deliberately small consumer module. It does not know
how research is calculated and it does not need access to research's SQLite database.

### Required Consumer Steps

1. Enumerate its configured inbox directory.
2. Parse and validate each item.
3. Reject an expired item.
4. Verify the item target equals the running bot identity.
5. Verify `alpha_id` has not already been accepted by that bot.
6. Verify the supplied symbol is supported by its live venue universe.
7. Convert it to the bot's existing normalized signal/intent form.
8. Send it through the existing rank/risk/execution path.
9. Persist `alpha_id`, `delivery_id`, source, entry expiry, and fixed TP mode
   into trade metadata before returning acceptance.
10. Write an atomic receipt and archive or delete the consumed inbox item.

The consumer must not invoke local strategy scoring or the local conviction
floor for a research signal. It does run existing portfolio, cooldown,
duplicate-position, circuit-breaker, size, leverage, notional, precision, and
order safety gates.

### Acceptance and Receipt

Acceptance means the bot has durably persisted a pending-limit or open-trade
record. It does not mean the exchange filled the entry.

The receipt includes `delivery_id`, `alpha_id`, acceptance status, reason,
bot trade ID, exchange order ID, and timestamp. The research adapter records
the receipt on its next poll. No synchronous RPC is needed.

Representative receipt statuses:

```text
accepted_pending_fill
accepted_open
skipped_expired
skipped_duplicate_alpha
skipped_unsupported_symbol
skipped_risk_gate
skipped_existing_position
failed_validation
failed_order_submission
```

## Normalization Into Existing Bot Paths

For Bybit, Bybit test, and MEXC, inject this pre-scored signal immediately
before the existing ranked execution call. Do not add it to the strategy
registry.

```python
UnifiedSignal(
    symbol=item["symbol"],
    direction=item["direction"],
    strategy_id=item["strategy_id"],
    meta={
        "price": item["entry_price"],
        "stopPrice": item["stop_loss"],
        "takeProfitPrice": item["take_profit"],
        "entryTag": item["entry_tag"],
        "source": item["source"],
        "alphaId": item["alpha_id"],
        "deliveryId": item["delivery_id"],
        "entryValidUntil": item["entry_valid_until"],
        "takeProfitMode": item["take_profit_mode"],
        "conviction": item["confidence"] * 100,
        "convictionScore": item["confidence"],
        "phase": 2
    },
)
```

The bot must call its normal ranked executor with a valid market context for
the symbol. If no context is available, skip the item as
`unsupported_or_unwarmed_symbol`; do not fabricate candle data.

## Entry, Stop, and Take-Profit Semantics

### Sizing

All targets size from the supplied structural stop:

```text
quantity = permitted_risk_usd / abs(entry_price - stop_loss)
```

Existing account and venue constraints may reduce or reject that quantity.
They must not increase permitted risk merely to meet a venue minimum notional.

### Entry

The entry is always a limit order at `entry_price`. The bot may round to venue
price precision only when the rounded result still preserves valid direction
geometry. If rounding makes the stop or target invalid, reject the delivery.

### Stop Loss

The initial stop is a reduce-only exchange order attached immediately after a
fill. Existing fail-closed behavior remains mandatory: if a filled position
cannot be protected, emergency-close it.

The existing babysitter may tighten the stop but must never widen it.

### Take Profit

`take_profit_mode = "fixed_full_close"` means create a reduce-only TP order
for the filled position quantity at `take_profit`.

For these research positions, disable the bots' normal 1R partial-profit
action. Otherwise a standing full-size TP order can exceed the remaining
position after the partial closes. The babysitter may continue stop-only
protection such as breakeven and favorable trailing moves.

If the venue cannot create the configured TP after a fill, emergency-close the
position just as for an SL placement failure. A research event is not complete
without both its exchange stop and fixed take-profit protection.

## Entry Expiry

`entry_valid_until` governs an unfilled research entry. At or after that
instant the bot cancels the order, marks the pending trade neutral, releases
its slot, and writes `entry_expired` in the receipt/metadata.

It does not close a filled position. A filled position remains managed by its
stop, fixed TP, and existing favorable-stop maintenance.

Do not overload the existing local-signal `signalBarTs` TTL. Local entries
retain their present `ENTRY_LIMIT_TTL_BARS` behavior. Research pending limits
must store and compare `entryValidUntil` explicitly.

## Per-Bot Implementation Seams

### Bybit and Bybit Test

Add a shared conceptual `ResearchSignalInbox` at the cycle point after local
signal scoring and before `RankedExecutor.execute_ranked`.

Required small changes:

1. Read and normalize inbox items into pre-scored `UnifiedSignal` instances.
2. Extend `RankedExecutor` to pass external `takeProfitPrice` and
   `entryValidUntil` into `CcxtExecutor.process_signal`.
3. Persist those values in `indicatorSnapshots` and pending-limit metadata.
4. Change pending-limit expiry logic only when `source == research_analyst`.
5. Place/reconcile a reduce-only fixed TP after fill.
6. Suppress 1R partial behavior for `takeProfitMode == fixed_full_close`.

Authoritative live symbol validation remains the existing loaded-market check
for active USDT-linear swaps.

### MEXC

Use the same consumer semantics and Python engine seam as Bybit. The MEXC
consumer validates against its own loaded CCXT MEXC swap market list, not the
Bybit list. Its exchange factory must remain MEXC-only.

### Propr

Use a target-specific consumer because Propr does not use the CCXT executor.

Required changes:

1. Reject when `is_tradeable(item["asset"])` is false.
2. Convert the symbol using the established Propr canonicalizer.
3. Call `ProprExecutor.process_signal` with the supplied limit entry and stop.
4. For research source only, calculate amount using stop distance rather than
   the current ATR proxy.
5. Carry and reconcile fixed TP and entry-expiry metadata through Propr's
   pending-limit lifecycle.
6. Disable the existing TP partial behavior for fixed-full-close research
   positions while retaining favorable stop tightening.

The current Propr ATR formula uses `atr1h * sqrt(24)` and falls back to 1% of
price. It does not require daily-candle backfill. It is still wrong for this
integration because it ignores the supplied invalidation and produces a
different risk amount from the other bots.

## Idempotency and Conflict Rules

- `alpha_id` is the per-target execution idempotency key.
- `strategy_id` is a reporting/risk tag only and is never unique.
- A bot must reject a second inbox item with an accepted `alpha_id`, including
  after restart.
- An existing open position or pending order for the same symbol wins over a
  research signal; skip the delivery rather than add to it.
- Research signals are not eligible for phase-two add-ons.
- A bot circuit breaker, cooldown, portfolio cap, or risk gate may reject an
  otherwise valid delivery. This is expected and must be recorded as a skip.
- The adapter does not retry a target after a terminal bot receipt. It only
  retries a failed atomic outbox write.

## Observability

Research must report per target:

```text
execution deliveries: pending / written / acknowledged / skipped / failed
skip reasons by target
oldest unacknowledged delivery age
```

Each bot must include in its normal trade metadata and event stream:

```text
source=research_analyst
alpha_id
delivery_id
research_strategy_id
entry_valid_until
take_profit_mode
```

## Test Plan

### Research Adapter

1. Valid long and short events produce deterministic target inbox JSON.
2. Invalid directional geometry is skipped.
3. Expired, inactive, unsupported entry type, and multi-target events are
   skipped with distinct reasons.
4. Re-running the adapter does not write a second delivery for the same
   `(alpha_id, target)`.
5. Atomic write produces no parseable partial file.
6. Static target symbol rejection writes a terminal skip record.

### Each CCXT Bot

1. A valid inbox item is converted to the expected pre-scored signal.
2. An unsupported loaded-market symbol is skipped without creating an order.
3. Duplicate `alpha_id` is skipped across a simulated restart.
4. A limit order uses the research entry and sizes from research stop distance.
5. The pending limit survives restart and cancels at `entryValidUntil`.
6. A filled entry creates exactly one reduce-only SL and one reduce-only fixed
   TP.
7. TP/SL placement failure after fill emergency-closes the position.
8. Babysitter does not take its standard 1R partial for a fixed-TP research
   position.

### Propr

1. Unsupported `is_tradeable` asset is skipped.
2. Crypto and HIP-3 symbol canonicalization is correct.
3. Research source sizing equals risk divided by supplied stop distance.
4. Non-research Propr signals retain their existing ATR sizing.
5. Pending entry expiry, TP/SL placement, and duplicate recovery match the
   CCXT behavioral contract.

## Rollout

1. Implement the research adapter with all targets disabled by default.
2. Enable only `bybit-test` in dry-run mode and verify delivery/receipt traces.
3. Enable real test-instance limits with a small fixed risk cap.
4. Compare fill, SL, TP, expiry, and idempotency behavior across restart.
5. Enable Bybit, then MEXC, then Propr one target at a time.
6. Keep a per-target feature flag so any integration can be disabled without
   disabling alpha production or Telegram publishing.
