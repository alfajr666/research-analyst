# Domain Context

## Market Universe

- **Performance pool**: the valid Bybit linear USDT-perpetual ticker universe
  available to the rotation snapshot.
- **Effective universe**: the cutoff-bound unexpired watchlist plus the four
  permanent assets: `BTC`, `ETH`, `PAXG`, and `QQQ`.
- **Subscription universe**: the assets whose market updates are currently
  delivered by the market-data gateway.
- **Performance source**: the source of point-in-time price performance used to
  rank the performance pool.
- **Rotation feed**: a versioned publication of the subscription universe and
  the evidence used to select it.

## Trading Policy

- **Strategy**: a symbol-dumb producer of candidates from the data delivered to
  it.
- **Candidate**: a strategy's proposed trade, before execution admission.
- **Venue capability policy**: the executor-owned rule defining which configured
  account may accept a canonical symbol and strategy. Research Analyst does not
  own this policy.
- **Bybit account profiles**: `bybit/hyro` accepts only `BTC`, `ETH`, `PAXG`,
  and `QQQ`, with all strategies eligible; `bybit/fundamo` accepts every
  strategy and symbol admitted by the venue. The executor is the final gate.
- **Research intent**: a strategy/symbol thesis with exchange and target
  metadata, but no producer-owned account capability decision.
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
- **Regime history cache**: regime-owned direct Bybit REST `1h` and `4h` history
  retained long enough to satisfy the regime scorer without waiting for live 5m
  accumulation; it is not the strategy market-data ledger.
- **Reversal activation gate**: the independent reversal-family unlock requiring
  confirmed regular RSI divergence, recent trend ADX, and negative ADX decay;
  it is not a trade trigger and does not deactivate other families.
- **Bootstrap readiness**: the per-asset state proving that regime `4h` history,
  live-evaluation `5m` history, and any required live stream prerequisites are
  complete before enforced evaluation.
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
