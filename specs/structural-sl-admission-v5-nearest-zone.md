# Admission-Owned Structural SL Admission v5: Nearest Zone

## Status

Implemented. This specification supersedes the v4 priority-only selector while
preserving its optional, process-start `STRUCTURAL_15M_ZONES_ENABLED` toggle.

## Selection Contract

Admission gathers eligible directional FVG and order-block zones from:

```text
4h direct regime history
1h direct regime history
15m market 5m history resampled in memory, when enabled
```

It selects the nearest eligible zone across the enabled timeframes. A zone is
eligible only when it is cutoff-bound, covered, active or partial, directional,
and on the permitted side of the candidate entry.

Distance is measured from the entry to the zone interval. An entry inside the
zone has zero distance. Outside entries use the distance to the nearest zone
boundary. Distance is divided by the selected timeframe's valid Wilder ATR14
before cross-timeframe comparison.

Equal normalized distances are resolved deterministically by timeframe priority
`4h`, then `1h`, then `15m`, followed by newest creation time and zone ID.

The selected zone is authoritative for structural stop validation. Admission
does not retry another zone after the selected zone fails geometry or ATR
buffer checks, and it never mutates the strategy's proposed stop.

The 15m toggle remains configurable. When disabled, no 15m data is loaded,
selected, or included in the proof.

15m structural evidence is admission-only and is not used as a candidate score
input. Candidate scoring is independently calculated from 4h/1h structural
context, market-data freshness, agreement, and contradiction evidence.
