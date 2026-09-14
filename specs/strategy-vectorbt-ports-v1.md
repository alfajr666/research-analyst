# vectorbt Engine-Handoff Ports v1

Implementation specification. Companion to the source-repo handoff documents
`engine_handoff/all_families_engine_specs.md` and
`engine_handoff/bb_locked_v1_engine_spec.md` (authority repo
`repo-final/vectorbt/strategies/*`, `repo-final/scripts/*`). Every parameter,
threshold, indicator formula, and execution idiom below is transcribed from the
authority code listed per strategy; nothing is invented. Where the Research
Analyst (RA) architecture cannot express a backtest behavior, the deviation is
listed explicitly under "Deviations" and is always on the execution side, never
the signal side.

## Operator decisions recorded 2026-09-14

1. All seven families are ported and enabled by default; the previous
   12-strategy production set is disabled by default (registered, opt-in via
   `STRATEGY_ENABLED_IDS`). This overrides the handoff's per-family verdicts
   for the rejected families.
2. `bb_locked_v1` exits are delivered as an executor bracket spec (handoff
   §9.1): the analyst freezes stop/tp1/tp2 and carries the arming/tp3/BEP
   contract in `metadata.bracket_spec`; the executor implements intrabar level
   fills.
3. The 30m-execution families (squeeze, KAMA, trend wall v5) evaluate on a
   causally grouped completed-30m frame built from canonical 5m bars, because
   the RA resampler only produces 15m/1h/4h.

## Shared kernels and independence

The seven ports are deliberately **self-contained** (operator decision
2026-09-14: "no shared parts from vectorbt; purely native Research Analyst").
They import only the repository's native engines:

- `strategy_features.build_feature_frame` — Bollinger (population std,
  `ddof=0`, matching the backtest convention), StochRSI (native 0–100 axis;
  the backtest's 0.20/0.80 thresholds scale to 20/80), Wilder ATR series.
- `strategy_v2_context` — `ema_series`, `wilder_rsi`, `stoch_rsi`,
  `resample_ohlcv`, cutoff-bound bar loading, `has_active_event`.
- `polars_indicators` — `wilder_atr_series`, `dmi_adx_series` (the seeded-RMA
  in-house ADX used by all native strategies).

Where the authority formula has no native kernel, a small private helper lives
inside the owning plugin (never shared across plugins): KAMA recurrence and
choppiness (kama plugin), 4h linear-regression slope (squeeze plugin), 30m/4h
and hourly groupers (squeeze/kama/trend-wall/mr plugins), rolling-24h VWAP
(trend-pullback), and the completed-HTF causal mapping used per plugin.

Authority-formula notes for the native kernels: the pandas
`ewm(alpha=1/length, adjust=False)` of the backtests equals the repository's
seeded Wilder RMA to float tolerance (first-value seed vs mean seed converges
within warmup; all comparisons are fail-closed on `None` warmup values), and
`dmi_adx_series` implements the repository's canonical ADX, which all native
strategies already use.

Backtest sizing (1% risk, 3% risk, KAMA ×3 multiplier, full-sleeve), fees, and
slippage are never reproduced; sizing and venue handling stay executor-side per
repository policy (handoff cross-family note 5).

## Registered plugins (all `5m` cadence, family per table)

| Strategy ID | Source authority | Family | Signal frame | Entry timing |
|---|---|---|---|---|
| `bb-tp-race-locked-v1` | `bb_stoch_tp_race_v1.py` | trend | 15m resampled + direct 1h EMA200 | next 15m open |
| `bb-squeeze-trend-v1` | `bb_squeeze_trend.py` | trend | 30m grouped; 4h grouped indicators | on fire bar (event execution) |
| `kama-trend-following-v1` | `kama_trend_following.py` | trend | 30m grouped + completed 4h KAMA | on signal bar close |
| `macd-ema-v1` | `macd_ema.py` | trend | direct 1h (hourly boundaries only) | next 1h open |
| `mr-vwap-locked-v1` | `backtest_vwap_rsi_prototype.py` (mr_locked_v1) | mean_reversion | 15m resampled + 1h regime | next 15m open |
| `trend-pullback-vwap-v1` | `trend_pullback_v1.py` | trend | 15m resampled (rolling_24h VWAP) | next 15m open |
| `trend-wall-v5` | `trend_wall.py` | trend | 30m grouped; completed 1h structure | next 30m open |

All seven are in `ADMISSION_STRATEGY_IDS` (hard admission, scorer, clash
resolution apply), in `config.PORTED_STRATEGY_IDS`, fanned out to Fundamo via
`PORTED_FUNDAMO_STRATEGY_IDS`, and enabled in the default
`STRATEGY_ENABLED_IDS`. New config constants are env-overridable with the
`BB_TP_RACE_*`, `BB_SQUEEZE_*`, `KAMA_TREND_*`, `MACD_EMA_*`, `MR_VWAP_*`,
`TREND_PULLBACK_*`, `TREND_WALL_V5_*` prefixes; defaults equal the frozen
backtest parameters.

## Strategy contracts (frozen parameters)

### bb-tp-race-locked-v1

Entry (long, all on the completed 15m candle): bullish StochRSI cross
(K.prev <= D.prev, K > D, K.prev <= 0.20), BB(20, 2.0, ddof=0) mid rising over
3 candles, close above mid, close in
`[upper − 0.50·ATR16, upper + 0.25·ATR16]`, close > completed-1h EMA200.
Short mirrored (`K.prev >= 0.80`, mid falling, lower-band envelope, close <
EMA200). Stop = signal low − 2·ATR16 (short: high + 2·ATR16), frozen. tp1 =
entry + 4·ATR48(signal), tp2 = entry + 2·ATR48 (mirrored), frozen for the life
of the trade. Entry fills at the next completed 15m open.

Executor bracket (`metadata.bracket_spec`): stop live intrabar every bar with
gap-through fills at open; on each completed-candle StochRSI %K extreme close
(≥ 0.80 or ≤ 0.20, either side) re-freeze tp3 = EMA7[extreme] ± 0.5·ATR16[extreme]
and arm exactly one candle of the intrabar race
{BEP after a 1·ATR16 favorable touch latch, tp3, tp2, tp1} — nearest touched
level wins, gap fills at open; shadow-BEP dip-to-entry exit on the armed candle
when BEP is not yet raceable; race consumed if untouched; trigger suppressed on
the entry candle; no breakeven move, no trailing, no time stop.

Deviations: (1) the race is executor-side, not replayed in the analyst — the
backtest's intrabar fills cannot exist in a completed-bar analyst, which is
exactly why the handoff §9.1 requires bracket orders; (2) tp3 starts unplaced
until the first trigger, so the initial executor bracket carries stop/tp1/tp2
only.

### bb-squeeze-trend-v1

All indicators on completed grouped-4h bars: BB(29, 1.82, ddof=0), Keltner
(EMA29 ± 1.56·ATR14), ADX14, linreg-slope(11) of close. Squeeze = BB inside KC;
fire = release after ≥ 4 consecutive squeeze bars; long fire additionally
requires ADX ≥ 19.11 and slope > 0 (short: slope < 0). The fire event executes
on the 30m bar that first receives the completed 4h bar (event execution, not
next-open). Stop = fill ∓ 1.74·ATR14 frozen at the fire bar; thereafter a
ratcheting trail at extreme ∓/± 3.71·ATR14 (continuously mapped 4h ATR), stop
only, no TP. Trail is a `strategy_exits` rule; RA never mutates the proposed
stop.

### kama-trend-following-v1

30m frame: close vs KAMA(14, 2, 30) (Jesse-Rust recurrence), ADX14 > 50,
Choppiness(14) < 50, BB-width% (20, 2.0) < 7.0; completed-4h KAMA filter
(close above for long, below for short). Cooldown: no new entry within 10 bars
of the last closed trade — replayed conservatively from completed bars (see
Deviations). Bracket: symmetric 2.5·ATR14(30m) stop and target, 1:1, intrabar,
gap-pessimistic, stop before target. The backtest ×3 leverage multiplier is
documented in `metadata.sizing_note` and deliberately not reproduced.

Deviations: the cooldown replays "last qualifying entry bar whose bracket could
not yet have resolved" because exact closed-trade timing requires portfolio
state the analyst does not own; `has_active_event` additionally prevents
same-direction re-firing. This is stricter than the backtest, never looser.

### macd-ema-v1

Long-only, direct 1h frame, hourly boundary gating (`cutoff.minute % 60`).
Entry: close > EMA100 (Jesse recurrence) AND MACD(12,26,9) line > signal AND
ATR valid. Stop = entry-candle low − 2·ATR14, frozen, executor bracket with
intrabar gap-pessimistic fills. Dataframe exit (`evaluate_exit`): close <
EMA100 AND line < signal at that candle's close. No TP. The source's
full-sleeve sizing stays executor-side.

### mr-vwap-locked-v1

15m frame (exact 15/15 groups via `resample_ohlcv`). VWAP anchor: persistent
highest completed-hourly base volume, seeded from the trailing 72 completed
hours (oldest tie wins), reset only on a strictly higher completed hourly
volume; VWAP and volume-weighted population σ from 15m HLC3 since anchor; bands
±1σ/±2σ. Setup: low < −2σ AND RSI14 < 40 (short mirrored). Confirmation
`rsi_then_price` within 3 completed bars: RSI recovers past 40/60, stays
recovered, then a close back inside the 2σ band. Regime gate:
|ΔVWAP(3h)/ATR14(1h)| < 0.25 with the same anchor at both endpoints. Entry:
next 15m open, price still beyond the frozen ±1σ bound. Stop: setup-through-
confirmation extreme ∓ 0.5·ATR14(15m), fixed. Target: confirmation VWAP or 2R,
whichever is nearer; planned R:R ≥ 1.2; no time exit. Only a confirmation on
the last completed 15m bar is emitted (older confirmations are stale).

### trend-pullback-vwap-v1

15m frame, VWAP mode `rolling_24h` (96 completed 15m HLC3 obs, volume-weighted,
population σ). Sequential per-side state machine replayed per evaluation:
regime (hourly VWAP slope beyond ±0.5 ATR-normalized plus two hourly closes on
the trend side) → impulse (close beyond ±1σ within the prior 8 bars) → setup
(pullback touch of ±1σ while holding VWAP side, RSI14 in [40,50] long /
[50,60] short) → pending ≤ 4 bars with extremes accumulating, cancelled on
regime loss, VWAP cross, or expiry → confirmation (RSI recrosses 50 and stays
recovered, then a close beyond the previous candle's high/low). Entry: next
15m open inside the 2σ band. Stop: accumulated pullback extreme ∓ 0.5·ATR14(15m).
Target: fixed 2R (runner policy `target_r_fixed=2.0`); planned R:R ≥ 1.2.
Intrabar 5m fills are executor-bracket semantics.

### trend-wall-v5

30m frame (grouped) with completed-1h structure mapped after bar close:
EMA99 "wall", EMA7 > EMA26 (long), ADX14 > 20, wall proximity < 1%, low ≤ wall
(long). Confirmation on the same 30m bar: RSI14 < 40 crossing up through
RSI-MA3 with volume ratio > 0.5 (short mirrored). Entry: next 30m open. Stop:
(confirmation high if below wall else wall) − 1·ATR16 (short mirrored).
Structure exit (`evaluate_exit`): 30m close beyond wall ± 0.5·ATR16. No TP.
Distinct from the legacy 15m `trend-wall-v1` plugin, which remains registered
but disabled.

## Data and scope

- Execution data: canonical completed 5m observations from `market.sqlite3`
  (gateway-owned), cutoff-bound. 15m frames via `resample_ohlcv` (exact groups
  only); 30m/4h frames via strategy-private exact-group groupers.
- 1h data: regime-owned direct history via `load_bars_for_interval`
  (`direct_htf_context`), never merged with resampled 5m. Missing or stale HTF
  fails closed (`DATA_FRESHNESS_MAX_SECONDS` per frame).
- Every plugin skips non-matching boundaries in `run_plugin`
  (`minute % 15/30/60`) so only the intended cutoffs evaluate.
- `has_active_event` prevents duplicate same-direction intents per asset.
- Admission (structural zones, scorer, clash resolution) applies unchanged;
  the ports add no new admission bypass. `trend-wall-v5` keeps the legacy
  `trend-wall-v1` family (trend) so regime hysteresis semantics are unchanged.

## Verification

```bash
PYTHONPATH=src/research_analyst venv/bin/python -m unittest discover -s tests
python3 -m compileall -q src tests
```

`tests/test_ported_strategies.py` covers registry/cadence/allowlist contracts,
signal-shape invariants (direction, stop/target geometry, bracket payloads),
stale-frame fail-closed, hourly gating, indicator parity (pandas ewm, ddof=0
std, KAMA recurrence, 30m grouping, HTF causality).

## Explicitly out of scope

- Exchange orders, fills, position state, sizing, leverage (executor-owned).
- The KAMA ×3 multiplier and backtest fee/slippage economics.
- Intrabar analytics inside the analyst; delivered via `bracket_spec` instead.
- Reconciliation replays against the Binance USDM archives (source-repo
  activity; the RA ports read Bybit canonical/direct data).
