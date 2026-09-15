# Venue-Owned Account Routing

## Status

Accepted design decision on 2026-09-15. Runtime migration is a follow-up
change and must not be inferred from this document alone.

## Boundary

Research Analyst owns strategy evaluation, candidate evidence, trade geometry,
and exchange-target publication. It does not own account selection, account
capabilities, or account-specific symbol policy.

The Bybit executor owns final profile admission and execution routing:

| Profile | Capability |
| --- | --- |
| `bybit/hyro` | `BTC`, `ETH`, `PAXG`, and `QQQ`; every strategy |
| `bybit/fundamo` | Every strategy and every venue-admitted symbol |

The executor rejects a profile/symbol combination before sizing or any order
call. Strategy identity is evidence and observability; it is not an account
authorization mechanism.

## Delivery Contract

The target migration must preserve:

- exact exchange/account/profile identity;
- per-profile idempotency and receipts;
- no duplicate acceptance when one thesis is eligible for multiple profiles;
- venue-owned symbol normalization and market availability checks;
- fail-closed behavior when the routing policy or venue state is unavailable;
- independent position caps, sizing, protection, and lifecycle state per profile.

During compatibility rollout, an account field may remain on an envelope as an
untrusted routing hint. The executor must enforce its profile capability policy
regardless of that hint. The final unscoped-delivery migration requires an
explicit bus/consumer contract for fan-out and must not be implemented by
silently changing a missing account to `hyro` or `fundamo`.

## Removed Policy

There is no strategy-owned asset class or special asset set. The four-symbol
Hyro restriction is a venue profile capability, not a Research Analyst
definition and not a strategy rule.

## Acceptance Checks

1. A non-listed asset tagged for `bybit/hyro` is rejected before an exchange
   order call.
2. A venue-admitted asset tagged for `bybit/fundamo` is eligible regardless of
   strategy ID.
3. The same thesis can be delivered to multiple eligible profiles without
   sharing journal state or duplicating a profile-local acceptance.
4. Research Analyst tests contain no account-capability or asset-policy gate.
5. Executors expose the selected profile and rejection reason in receipts and
   health/operational logs.
