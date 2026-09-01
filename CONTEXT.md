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

## Ownership

- **Gateway**: the owner of market subscriptions and market-database writes.
- **Evaluator**: the owner of strategy evaluation and candidate production.
- **Executor**: the owner of sizing, protection, execution, and receipts.
