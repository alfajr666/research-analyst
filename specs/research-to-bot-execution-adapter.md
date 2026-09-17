# Venue Adapter Retirement Record

Status: Retired and superseded

The former filesystem execution-adapter design is not part of Research
Analyst. The repository does not create venue inbox files, reconcile venue
receipts, or maintain venue-delivery state.

The only supported handoff is:

```text
admitted candidate
  -> TradeIntent schema v2 with admission proof
  -> shared SQLite intent bus
  -> independently owned venue executor
```

The shared bus owns delivery identity, target fan-out, leasing, retries, and
receipt visibility. Venue executors own credentials, capability checks, sizing,
precision, orders, fills, protection, and position state.

Historical databases may still contain retired `execution_deliveries` rows.
Current code neither creates nor reads that table, and this cleanup does not
drop production data online.
