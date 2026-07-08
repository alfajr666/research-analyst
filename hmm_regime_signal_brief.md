# HMM + VWAP Regime Detection — Signal Bot Brief

## What We're Building

A **per-symbol signal generation layer** that combines VWAP (price location filter) with a Hidden Markov Model (market character filter) to emit high-confidence long/short/no-signal outputs across a crypto universe.

This is not an execution bot. The signal is the product.

---

## Why HMM Over Pure VWAP

| | VWAP | HMM |
|---|---|---|
| What it answers | Where is price? | What kind of market is this? |
| Output | Continuous deviation | State probability distribution |
| Regimes it sees | Effectively one (above/below) | Multiple: trending up/down, ranging, high-vol |
| Confidence signal | None | Yes — per-state probability |
| Adapts per symbol | No | Yes — trained per instrument |

**VWAP is not replaced. It becomes one of HMM's input features, and also a standalone filter in the signal condition.**

---

## Input Features (Per Symbol, Per Bar)

| Feature | Why |
|---|---|
| Log returns | Core return signal |
| Realized volatility (rolling 20-bar) | Regime character |
| VWAP deviation (% from VWAP) | Price location + momentum |
| Volume z-score | Conviction behind moves |
| High-low range normalized by close | Intrabar volatility |

Feed these as a multivariate observation vector into the HMM at each timestep.

---

## Model Design

- **Hidden states:** 3–4 (e.g., trending up, trending down, ranging, high-vol/unclear)
- **Emission model:** Gaussian HMM (standard starting point for returns + vol features)
- **Training:** Sliding window retrain on last N bars (e.g., 500 bars), per symbol
- **Retrain schedule:** Periodic (daily or weekly), not per bar
- **Inference:** Run every bar on the stored fitted model — cheap
- **Library:** `hmmlearn` (Python)
- **Scope:** One HMM instance per symbol — parameters are instrument-specific

---

## Signal Logic

```
Per bar, per symbol:

1. Compute features → [log_return, realized_vol, vwap_dev, vol_zscore, hl_range]
2. Feed to stored HMM → get state probabilities [p0, p1, p2, p3]
3. Identify dominant regime + confidence

LONG signal:
  price > VWAP
  AND P(trending_up) > 0.65

SHORT signal:
  price < VWAP
  AND P(trending_down) > 0.65

NO SIGNAL:
  HMM regime unclear (no state > 0.60)
  OR regime is high-vol / ranging
  OR price/regime conditions conflict
```

Use **probability threshold**, not hard state label. Regime boundaries are where bad trades happen — soft thresholds handle this correctly.

---

## Signal Output Schema

```json
{
  "symbol": "BTCUSDT",
  "timestamp": "2026-07-04T10:00:00Z",
  "signal": "long | short | no_signal",
  "regime": "trending_up | trending_down | ranging | high_vol",
  "regime_confidence": 0.72,
  "price_vs_vwap": "above | below | at",
  "trigger_price": 65420
}
```

The `regime_confidence` field is a first-class output — not metadata. Consumers of the signal apply their own threshold.

---

## Architecture Flow

```
Symbol universe (~500 symbols)
        │
        ▼
Feature computation (per bar)
        │
        ▼
HMM inference (stored model per symbol)
        │
        ▼
Regime filter: P(regime) > threshold?
        │
   YES  │  NO
        │   └──► no_signal
        ▼
VWAP filter: price above/below?
        │
   YES  │  NO
        │   └──► no_signal
        ▼
Emit signal with metadata
```

HMM acts as a **universe pre-filter** — reducing ~500 symbols to those in a favorable regime before any entry logic runs.

---

## Practical Caveats

- Minimum ~300–500 bars per regime for stable parameter learning
- Number of hidden states (k) is a manual hyperparameter — start with 3, validate
- Crypto violates stationarity during structural breaks → periodic retraining is non-negotiable
- Gaussian HMM assumes normally distributed emissions — log returns are close enough for a first pass; Student-t HMM is a more robust upgrade
- At 500 symbols, training is batch-heavy; schedule retrains during off-peak hours

---

## Next Steps

1. Pick a pilot universe (e.g., top 20 by volume) for initial validation
2. Label historical regimes manually on 2–3 symbols to sanity-check HMM state assignments
3. Backtest signal quality: hit rate, regime persistence, false positive rate during high-vol periods
4. Define retraining cadence and model storage strategy
5. Build signal output layer (webhook, flat file, or stream)
