# Domain Context

## Market Universe

- **Approved universe**: the canonical set of currently approved tradeable
  assets.
- **Subscription universe**: the assets whose market updates are currently
  delivered by the market-data gateway.
- **Performance source**: the source of point-in-time price performance used to
  rank the approved universe.
- **Rotation feed**: a versioned publication of the subscription universe and
  the evidence used to select it.

## Trading Policy

- **Strategy**: a symbol-dumb producer of candidates from the data delivered to
  it.
- **Candidate**: a strategy's proposed trade, before execution admission.
- **Symbol-account-strategy policy**: the rule defining which account may trade
  which canonical symbol for a given strategy.
- **Hard gate**: a deterministic rejection that prevents a candidate from
  proceeding to scoring or execution publication.
- **Market regime**: the current completed-bar behavior of an asset, such as
  trend, range, reversal, or shock; it is not implied by the UTC session.
- **Session context**: the UTC time window containing an evaluation cutoff. Any
  session may contain any market regime.
- **Regime-session gate**: the first, per-asset eligibility decision that combines
  session context, current regime score, and data readiness before strategy
  evaluation begins.
- **Regime observation**: the immutable, point-in-time score and gate result for
  one asset and one completed cutoff.
- **Structural reference**: a confirmed, point-in-time market feature that a
  candidate declares as the level whose violation invalidates its setup, such
  as a swing or a selected imbalance zone.
- **Structural stop**: the candidate-proposed stop level placed beyond its
  declared structural reference, with a small execution buffer. The admission
  layer validates it but never moves it.
- **Structural-stop admission**: an independent deterministic pass/reject
  check that the proposed structural stop clears its declared reference in the
  correct direction and is not unreasonably distant from that reference.

## Ownership

- **Gateway**: the owner of market subscriptions and market-database writes.
- **Evaluator**: the owner of strategy evaluation and candidate production.
- **Executor**: the owner of sizing, protection, execution, and receipts.
