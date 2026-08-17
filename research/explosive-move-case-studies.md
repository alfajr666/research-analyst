# Explosive-Move Case Studies: VELVET, BEAT, LAB, and AKE

**Research date:** 2026-08-16 UTC
**Scope:** the roughly 30 days preceding the relevant August 2026 moves, extended below with repeated historical episodes. This is an evidence report, not investment advice or a claim that a move was knowable in advance.

## Executive conclusion

The requested identities resolve to **Velvet Capital (VELVET)**, **Audiera (BEAT)**, **LAB / lab.pro (LAB)**, and **Akedo (AKE)**. Project documentation and exchange/API evidence support the market mappings used here. There is no comparably strong primary evidence that all three original August cases had an *upward* explosive move: Gate's primary OHLCV records show a large upside move in AKE, a July-to-early-August run and then a violent reversal in BEAT, and a continued downside move in LAB. LAB should therefore be used as a negative/control case, not retrospectively relabeled as a bullish case study.

| Asset | Relevant observed move | Preliminary classification | Predictability assessment |
|---|---:|---|---|
| VELVET | Repeated 2025-26 upside bursts, including $0.4352 open to $1.3000 intraday high Aug. 10-14 | Constructive-base breakouts, with one event-adjacent but non-causal match | Structure and turnover are observable; no dated catalyst explains most episodes |
| BEAT | Gate close $2.5126 on Jul. 15 to intraday high $6.00 on Aug. 1, then $0.3773 on Aug. 15 | Momentum/leverage-sensitive run followed by distribution/liquidation | Partly observable in spot/futures volume and breakouts; the size and reversal were catalyst/liquidity dependent |
| LAB | Gate close $0.1550 on Jul. 23 to $0.0857 on Aug. 15 | Explosive **downside** / supply-risk control | A scheduled supply event is in principle knowable, but the exact sell-off magnitude is not |
| AKE | Gate close $0.0040616 on Aug. 12 to $0.0105999 on Aug. 15; high $0.0162681 | Explosive upside, preceded by earlier July trend leg | Best price/volume setup of the three; no primary project announcement found that fully explains the Aug. 14-15 acceleration |

## Method and evidence hierarchy

1. Daily OHLCV below is from Gate's unauthenticated spot REST API, using the named `*_USDT` pair and UTC daily buckets. Gate returns `[timestamp, quote_volume, close, high, low, open, base_volume, is_closed]`. The live queries are cited in each section so results are reproducible. Values called "volume" are quote-volume in USDT, not aggregate cross-exchange volume.
2. Gate USDT perpetual candles were queried where the contract existed, as a limited derivatives proxy. They do **not** provide funding, open interest, liquidations, order-book depth, or all-venue positioning.
3. Project tokenomics come from official project documentation where available. Exchange listing notices are first-party exchange sources. BscScan contract pages establish the observed contract, but are not issuer announcements.
4. Secondary stories were used only as leads. A claim from one is not relied on unless the underlying primary source was located. In particular, social-post claims about a BEAT revenue/burn update, an AKE giveaway, and a LAB August claim/unlock were not promoted to facts when a stable official post or on-chain distribution record was not found during this research pass.

## Identity resolution and availability

### Audiera (BEAT)

* **Resolved identity:** Audiera's BEP-20 token, **BEAT**, contract `0xcf3232B85b43BCa90E51D38cc06Cc8bB8C8A3E36`. Gate's first-party listing notice names Audiera, gives that contract, and says BEAT/USDT spot trading began on **2025-11-01 10:00 UTC**. [Gate listing notice](https://www.gate.com/announcements/article/47925)
* **Venue availability for this study:** Gate `BEAT_USDT` spot; Gate `BEAT_USDT` USDT perpetual data were available from the API. This establishes availability on that venue, not global availability or a recommended venue.
* **Official supply design:** Audiera documentation states 1,000,000,000 total BEAT. It specifies 40% community, 15% foundation, 13.0733334% advisors/angels, 10% marketing/operations, 8% team, 7.9266666% early-user airdrop, 4% liquidity, and 2% further-user airdrop; it also specifies the relevant cliffs/linear schedules. [Audiera token economics](https://docs.audiera.fi/protocol-design/system-architecture/economic-flow/editor)

### LAB (LAB / lab.pro)

* **Resolved identity:** lab.pro's BNB Smart Chain token, **LAB**, contract `0x7ec43Cf65F1663F820427C62A5780B8F2E25593A`. The contract page identifies the token as LAB and describes the project as a multi-chain trading ecosystem. [BscScan contract](https://bscscan.com/token/0x7ec43cf65f1663f820427c62a5780b8f2e25593a)
* **Independent exchange confirmation:** Poloniex's listing notice gives the same contract, labels it LAB, links `https://lab.pro/`, and scheduled LAB/USDT trading for **2025-10-16 12:00 UTC**. [Poloniex listing notice](https://support.poloniex.com/hc/en-us/articles/35671710910743-New-Listing-LAB-LAB)
* **Venue availability for this study:** Gate `LAB_USDT` spot and Gate `LAB_USDT` USDT perpetual candles were returned. This confirms API-visible historical availability only.
* **Tokenomics uncertainty:** I did not locate a stable official lab.pro tokenomics/unlock publication. Tokenomist's tracker is useful for discovery but is not issuer primary evidence; it reports a 1bn total and allocations which should be independently verified before production use. [Tokenomist tracker, non-primary](https://tokenomist.ai/lab/tokenomics)

### Akedo (AKE)

* **Resolved identity:** Akedo / AKEDO's BNB Smart Chain token, **AKE**, contract `0x2c3a8Ee94dDD97244a93Bc48298f97d2C412F7Db`. This identity is supported by KuCoin's first-party announcement: AKE/USDT spot trading began **2025-08-21 12:00 UTC**. [KuCoin Spotlight announcement](https://www.kucoin.com/news/articles/kucoin-spotlight-s-triumphant-return-partnering-with-akedo-pioneering-a-price-guarantee-and-ushering-in-a-new-era-of-ieos)
* **Venue availability for this study:** Gate `AKE_USDT` spot and `AKE_USDT` USDT perpetual API series were available. Kraken separately announced AKE trading live on **2025-08-22**, subject to its availability restrictions. [Kraken availability notice](https://blog.kraken.com/product/asset-listings/akedo-is-available-for-trading)
* **Tokenomics uncertainty:** An issuer-hosted vesting source was not located. Tokenomics.com (non-primary) reports 100bn maximum supply, 25% investors, 15% early contributors, 5% advisors, and an Aug. 21, 2026 scheduled 2.1078bn-token unlock. Treat this only as a monitoring lead until checked against Akedo's contracts/official release documents. [Tokenomics.com tracker, non-primary](https://app.tokenomics.com/tokenomics/akedo-games/unlocks)

## Case study: Audiera (BEAT)

### Market structure in the pre-move month

Gate spot fell from a Jul. 15 close of **$2.5126** to a Jul. 19 close of **$2.3436**, then broke higher: $3.0120 on Jul. 23, $3.2394 on Jul. 24, and $3.5187 on Jul. 26. Jul. 24 quote volume was **$9.64m**, versus **$3.04m** on Jul. 15. Jul. 27 printed a wide range ($2.4447-$4.7079) and $12.03m quote volume, then the market reclaimed and closed $4.6230 on Aug. 1 (high $6.00, $10.51m quote volume).

Source: [Gate BEAT/USDT daily spot candles, Jul. 15-Aug. 16](https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair=BEAT_USDT&interval=1d&from=1784073600&to=1786838400).

The Gate perpetual series broadly confirms the same regime change: Jul. 24 notional was **$13.85m**, Jul. 27 **$26.17m**, Aug. 1 **$29.84m**; the latter daily high was $6.30. [Gate BEAT USDT perpetual daily candles](https://api.gateio.ws/api/v4/futures/usdt/candlesticks?contract=BEAT_USDT&interval=1d&from=1784073600&to=1786838400)

### Catalyst and reversal timeline

* **Before Aug. 1:** price/volume expansion was visible in the primary candle series. The official tokenomics document describes ongoing emissions, but does not itself date each future release.
* **Aug. 1:** third-party vesting trackers list an approximately 21.25m BEAT scheduled release. This is directionally consistent with the official vesting design but is **not treated as verified issuer evidence** here. [Tracker lead, non-primary](https://app.tokenomics.com/tokenomics/audiera/unlocks)
* **Aug. 2-10:** Gate spot first fell to $1.9430 on Aug. 6, rebounded to $3.6450 on Aug. 9, then collapsed. This is incompatible with a simple one-way catalyst explanation.
* **Aug. 10-15:** closing price fell $3.6450 to $0.3773. Aug. 14 volume was $24.28m and Aug. 15 volume $24.33m. The perpetual series shows $57.83m notional Aug. 10, $71.25m Aug. 11, and $39.65m Aug. 15, consistent with exceptionally stressed trading but insufficient to identify liquidations or the causal trader cohort.

### Assessment

**Partly price/derivatives-predictable, not reliably catalyst-predictable.** A model could have seen the breakout, range expansion, and the sharp growth in spot/perpetual turnover. It could not, from these data alone, determine whether the move would extend, whether a reported burn/revenue narrative was valid, or when the reversal would occur. Emission exposure should have been treated as a known conditional risk rather than a directional signal.

## Case study: LAB (LAB)

### Market structure in the pre-move month

LAB had already declined from a Jul. 15 close of **$0.1550** to **$0.1343** on Aug. 1. A short recovery topped at $0.1456 on Aug. 4. It then made lower closes to $0.1143 on Aug. 12, $0.1022 on Aug. 13, and **$0.0857 on Aug. 15**. The Aug. 15 low was **$0.0820**.

Quote-volume moved from $1.71m on Jul. 31 to $7.14m on Aug. 1, then spiked to **$10.44m on Aug. 15**, while the futures notional reached $104.64m on that day in the data available earlier in the month. This is an adverse, high-turnover break rather than an upside expansion.

Sources: [Gate LAB/USDT daily spot candles](https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair=LAB_USDT&interval=1d&from=1784073600&to=1786838400); [Gate LAB USDT perpetual daily candles, Jul. 1-Aug. 1](https://api.gateio.ws/api/v4/futures/usdt/candlesticks?contract=LAB_USDT&interval=1d&from=1782864000&to=1785542400).

### Catalyst timeline and limitations

* **Observed:** prior downtrend, failed recovery, then expanding selling volume are directly recorded by Gate.
* **Reported but not accepted as established:** secondary reporting points to a large Aug. 14 unlock/claim. The accessible tokenomics tracker has internally inconsistent current-state language and is not an official issuer document. It should be corroborated against on-chain vesting/distributor contracts and a dated lab.pro announcement before triggering production logic.
* **Conclusion:** the primary evidence supports a supply/liquidity-risk case, but does not prove that a specific unlock caused the Aug. 15 candle.

### Assessment

**Price structure was observable; claimed catalyst remains unverified.** A rule based on lower highs/lows and downside volume expansion could flag elevated downside risk. It should not label the event as an "explosive bullish move" or train an upside alpha model on it.

## Case study: Akedo (AKE)

### Market structure in the pre-move month

AKE's sequence is the cleanest acceleration in the set. Gate spot rose from a Jul. 14 low/close region near **$0.0001896** to $0.0020004 on Jul. 18, then to $0.0040406 on Jul. 27 and $0.0059716 on Aug. 1. It consolidated around $0.00393-$0.00427 from Aug. 3-12. On Aug. 13 it closed **$0.0067907** (high $0.0095669) on **$18.95m** quote volume, versus $0.683m on Aug. 12. On Aug. 14 it closed **$0.0105999**, high **$0.0162681**, on **$34.64m** quote volume. This is +160.9% from the Aug. 12 close to the Aug. 14 close, and +300.0% from the Aug. 12 close to the Aug. 14 intraday high.

The available Gate perpetual feed independently shows Aug. 13 close $0.0067778/high $0.0106012/$80.98m notional and Aug. 14 close $0.0106044/high $0.0165899/$148.49m notional. That is a nearly 28x increase in notional versus Aug. 12's $2.87m. It supports broad derivatives participation but does not identify whether the net impulse was short covering, new longs, market-maker flow, or cross-venue arbitrage.

Sources: [Gate AKE/USDT daily spot candles](https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair=AKE_USDT&interval=1d&from=1784073600&to=1786838400); [Gate AKE USDT perpetual daily candles](https://api.gateio.ws/api/v4/futures/usdt/candlesticks?contract=AKE_USDT&interval=1d&from=1784073600&to=1786838400).

### Catalyst timeline

* **Jul. 14-31:** a first, persistent upside leg already existed. Thus the August move was an acceleration in an established trend, not a cold start.
* **Aug. 3-12:** consolidation held well above July levels while volume contracted. This is a measurable compression regime.
* **Aug. 13-14:** breakout, large spot-volume expansion, and very large perpetual notional expansion occurred together.
* **Project/news catalyst:** secondary leads point to an Akedo community campaign and an ADODO Node buyback/verification process. No stable dated official announcement with enough detail to attribute the Aug. 13-14 move was retrieved. These remain hypotheses, not evidence. The official project site should be archived and its announcement URLs incorporated before treating any event as a catalyst label.

### Assessment

**More price/derivatives-predictable than the other two, with catalyst uncertainty.** A deterministic system could have flagged: prior trend, multi-day volatility/volume compression, break above the Aug. 3-12 range, and simultaneous spot/perpetual activity expansion. It could not safely forecast the 4x intraday range or attribute it to a specific announcement without a timestamped primary source.

## Cross-case comparison

| Dimension | BEAT | LAB | AKE |
|---|---|---|---|
| Pre-event structure | Breakout then unstable wide-range trading | Persistent lower highs/lows | Trend, consolidation, then breakout |
| Volume confirmation | Present but followed by reversal | Selling-volume expansion | Strongest spot and perpetual expansion |
| Scheduled supply relevance | Official long vesting exists; August amount needs confirmation | Reported schedule is not primary-verified | Large Aug. 21 unlock is a monitoring hypothesis, not a verified source in this report |
| Clean primary catalyst | Not located | Not located | Not located |
| Correct label | Volatile round trip | Negative/control | Upside momentum case |

The recurring measurable pattern is not "announcement causes rally." It is **a low/mid-cap asset moving from a compressed or trending state into a simultaneous spot and perpetual turnover expansion**. The contrasting outcomes show why catalyst text and supply calendars must be separate variables, not retrospective proof of direction.

## Candidate falsifiable features for the alpha producer

These are hypotheses with explicit tests, not production signals yet.

1. **Range-break feature:** close above the highest close of the preceding 7 or 10 completed UTC days. Test whether AKE-like breakouts outperform matched assets after costs; record failures such as BEAT's reversal.
2. **Spot volume shock:** current daily Gate quote-volume divided by the trailing 7-day median. Candidate threshold: >=3. AKE was approximately 27.8x on Aug. 13 versus Aug. 12, but the denominator should be a completed-window median in implementation, not a single-day comparison.
3. **Cross-market confirmation:** spot volume shock and perpetual notional shock occur on the same day. Test whether this improves continuation versus spot-only spikes. Do not call this "short squeeze" absent funding, OI, and liquidation data.
4. **Compression-to-expansion:** 5-7 day realized range/ATR contracts below its 30-day percentile, followed by a close outside the compression range with volume confirmation. AKE's Aug. 3-12 interval is the motivating example.
5. **Supply-event distance:** days to the next *primary-source-verified* unlock, unlock amount divided by circulating supply, and unlock amount divided by venue 30-day median quote volume. Test separately for positive and negative returns; do not assume unlocks are bearish.
6. **Reversal risk:** intraday range, close-to-low position, and volume shock after a parabolic advance. BEAT shows why a continuation model must have an explicit invalidation/exit rule.
7. **Venue coverage filter:** number of independently confirmed liquid spot/perpetual venues and cross-venue price dispersion. Gate-only observations are vulnerable to venue-specific liquidity and data artifacts.
8. **Event evidence quality:** categorical feature: no event, official dated announcement, official on-chain transaction, exchange announcement, or secondary-only report. Backtests should exclude or separately score secondary-only catalyst labels.

### Required falsification protocol

* Define the universe and availability timestamp before calculating any feature; do not include a pair before its first official/exchange-confirmed trade time.
* Use only information available before the forecast cutoff; candle close data cannot generate a same-candle tradable signal without a documented execution assumption.
* Evaluate forward 1d, 3d, and 7d returns net of fees/slippage, separately for longs and shorts.
* Compare against matched controls by market cap/liquidity/venue count and report hit rate, median return, maximum adverse excursion, and tail loss.
* Stratify all results by verified supply-event proximity and by whether derivative data include funding/OI. Without those fields, "derivatives-confirmed" means turnover-confirmed only.

## Known limitations

* This report covers Gate daily candles, not consolidated prices. Pair-specific close/high/low and volume can differ materially across exchanges.
* API results may change for an unclosed current day; the Aug. 16 candle was marked `false` and is not used for completed-day conclusions.
* No primary full historical funding, OI, liquidation, wallet-flow, market-maker, or order-book dataset was available in the sources retrieved. Causality cannot be inferred from price/volume co-movement.
* Official project X/Telegram material can be deleted or login-gated. No unarchived social claim is used as evidence.
* The available third-party unlock pages conflict with or go beyond issuer documentation in places. Their figures are leads for validation, not inputs to a live alpha producer.

## Freqtrade Historical Control Corpus

The named AKE, BEAT, LAB, and VELVET cases motivate the research, but they are
not a sufficient training set. The local Freqtrade Binance USDT-perpetual archive
at `/home/ubuntu/freqtrade-trading-bot/backtest/pair_trading/freqtrade_cache_91/binanceusdm/futures`
contains 91 fifteen-minute pairs from 2025-01-01 through 2026-06-30. It does not
contain the named post-June AKE/BEAT/LAB/VELVET contracts, so CoinAnalyze remains
the source for those case studies.

### Raw expansion labels are contaminated

An initial corpus definition found a non-overlapping episode when a pair's
maximum high in the following four hours was at least 12% above the observation
close. It produced 1,778 episodes across 89 assets. This is deliberately a
broad discovery label, not an alpha target.

The distribution disproves the idea that every large upward move is AKE-like:

* The median four-day return before a raw episode was -2.8%; median BTC-relative
  return was -1.1%.
* The median three-day base range was 26.6%, and its range was expanding versus
  the preceding three days (median ratio 1.17), not compressing.
* 78 raw episodes shared 2025-10-10, a market-wide stress/reversal period. A
  per-asset cooldown does not remove common-market shocks.

These are often liquidation bounces, short-covering reversals, or broad-market
dislocations. Training an ignition ranker on this entire label set would teach it
to chase disorder rather than identify constructive latent pressure.

### Preliminary constructive subset

For research segmentation only, the following pre-event filter selected 140
episodes across 53 assets:

```text
4d asset return > 5%
4d BTC-relative return > 0
3d base range / preceding 3d range < 0.90
current 15m volume / prior-day median volume < 3
next-4h maximum favorable move >= 12%
```

This set includes repeated episodes in assets such as CFX, JTO, FARTCOIN,
POPCAT, ZEC, and VIRTUAL. It is a candidate research cohort, not a production
rule: the conditions were selected after inspecting the same data and therefore
need walk-forward validation against matched non-event controls.

### Implication for `explosion_ignition`

The research target needs at least two separate labels:

| Label | Pre-event state | Role |
| --- | --- | --- |
| `constructive_expansion` | Trend, relative strength, compression, then expansion | Positive class for AKE-like ignition research |
| `shock_or_reversal_expansion` | Negative/weak trend or expanding range before a sharp move | Explicit exclusion/control class |

The alpha producer should rank the probability of the first class, not the
generic probability of a large candle. The next dataset build must add matched
non-event controls for every selected episode and evaluate ranking precision by
date, asset, and liquidity bucket.

## Repeated historical episodes

This section extends the August window with distinct completed-day episodes from the same Gate spot feed. An episode is an observed high-range, high-turnover expansion, not a claim that its cause is known. `Base` means a multi-day comparatively narrow range before the break; `latent pressure` means an already-rising, high-activity regime rather than a quiet base; `event-driven` means the episode coincides with a dated, primary-source event or a new-market period. The latter describes timing, not causality.

### VELVET (Velvet Capital)

* **Aug. 6-8, 2025: $0.0448 close to $0.1184 intraday high.** After roughly two weeks mostly between $0.04 and $0.07, the Aug. 6-7 quote volume rose from $0.21m to $4.93m. **Classification: constructive base.** No dated official announcement was located for this interval, so a catalyst claim is **unverified**. [Gate spot candles](https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair=VELVET_USDT&interval=1d&from=1754352000&to=1754784000)
* **Jun. 3-11, 2026: $0.0932 close to $1.8988 intraday high.** The move began from a $0.09-$0.12 area and spot quote volume grew from $0.07m on Jun. 3 to $21.92m on Jun. 11. **Classification: constructive base, then event-adjacent acceleration.** Velvet published its Trade[XYZ] integration announcement on Jun. 2. That announcement and its date are **verified**, but the available evidence does **not** establish that it caused the token move. [Gate spot candles](https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair=VELVET_USDT&interval=1d&from=1780358400&to=1781481600); [official Velvet announcement](https://blog.velvet.capital/p/velvet-x-tradexyz-access-global-markets)
* **Aug. 10-14, 2026: $0.4352 open to $1.3000 intraday high.** This occurred after a $0.40-$0.48 consolidation; Aug. 10-13 quote volume was $4.60m, $2.85m, $1.34m, then $7.09m. **Classification: constructive base.** No primary dated catalyst was retrieved. Velvet's official tokenomics document verifies staking rewards, liquidity provision, and an emissions design, but it does not date a release that explains this window. [Gate spot candles](https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair=VELVET_USDT&interval=1d&from=1786233600&to=1786924800); [official tokenomics](https://docs.velvet.capital/governance/tokenomics)

### Audiera (BEAT)

* **Nov. 14-20, 2025: $0.1581 open to $1.6747 intraday high.** Gate quote volume reached $21.74m on Nov. 14 and $16.75m on Nov. 20. The sequence had already advanced from $0.13-$0.16 in the preceding days, so it was not a low-volatility base. **Classification: latent pressure / continuation.** Gate's Nov. 1 listing notice verifies venue availability, not a Nov. 14-20 project catalyst; no causal announcement was found. [Gate spot candles](https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair=BEAT_USDT&interval=1d&from=1762819200&to=1763856000); [Gate listing notice](https://www.gate.com/announcements/article/47925)
* **Dec. 4-11, 2025: $0.8314 open to $3.4989 intraday high.** Daily quote volume rose from $3.71m on Dec. 4 to $18.57m on Dec. 11 after an already-active November advance. **Classification: latent pressure / continuation, not a clean base.** Audiera's official document verifies its allocation and cliffs, but does not identify a dated December release or announcement as the cause. [Gate spot candles](https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair=BEAT_USDT&interval=1d&from=1764633600&to=1765843200); [official token economics](https://docs.audiera.fi/protocol-design/system-architecture/economic-flow/editor)
* **Jul. 23-Aug. 1, 2026: $3.0120 close to $6.0000 intraday high.** This is the earlier case study's run: quote volume reached $9.64m on Jul. 24 and $10.51m on Aug. 1, after a short $2.34-$2.63 stabilization. **Classification: shallow constructive base, but unstable.** The subsequent reversal makes this a failed-continuation example, not evidence that a base implies durable upside. Its reported August unlock remains **unverified** as stated above. [Gate spot candles](https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair=BEAT_USDT&interval=1d&from=1784073600&to=1785542400)

### LAB (lab.pro)

* **Oct. 14-18, 2025: $0.0100 open to $0.3864 intraday high.** This was the initial Gate-visible trading period, with daily quote volume between $2.70m and $7.82m. **Classification: event-driven/new-market regime; no pre-move base is measurable.** Poloniex's first-party notice verifies an Oct. 16 LAB/USDT listing, but it does not prove the Gate price action was caused by that listing. [Gate spot candles](https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair=LAB_USDT&interval=1d&from=1760400000&to=1760832000); [Poloniex listing notice](https://support.poloniex.com/hc/en-us/articles/35671710910743-New-Listing-LAB-LAB)
* **Apr. 6-15, 2026: $0.2482 open to $0.7400 intraday high.** Price left a roughly $0.19-$0.25 area while quote volume expanded from $0.24m on Apr. 6 to $2.58m on Apr. 12. **Classification: constructive base.** No stable official LAB tokenomics, unlock, or dated product announcement was retrieved that attributes this move; catalyst claims are **unverified**. [Gate spot candles](https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair=LAB_USDT&interval=1d&from=1775260800&to=1776556800); [official LAB documentation](https://docs.lab.pro/intro/introducing-lab-terminal-your-new-way-to-trade)
* **May. 1-10, 2026: $0.6923 open to $4.7023 intraday high.** Quote volume increased from $3.93m on May. 1 to $102.28m on May. 3 and remained tens of millions of USDT through May. 10. **Classification: latent pressure / event-like turnover burst, not a constructive base.** The fast reversal and lack of a primary event source make it unsuitable for a bullish-catalyst label. [Gate spot candles](https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair=LAB_USDT&interval=1d&from=1777507200&to=1778544000)

### Akedo (AKE), comparison asset

* **Apr. 9-16, 2026: $0.0002275 open to $0.0013192 intraday high.** The preceding prices were mostly $0.00020-$0.00029; quote volume expanded from $0.43m on Apr. 9 to $10.10m on Apr. 16. **Classification: constructive base.** No primary dated Akedo announcement was retrieved for attribution. [Gate spot candles](https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair=AKE_USDT&interval=1d&from=1775347200&to=1776556800)
* **Jul. 14-18, 2026: $0.0001896 close to $0.0021175 intraday high.** This followed a long $0.00018-$0.00024 region. Quote volume grew from $0.77m on Jul. 14 to $15.03m on Jul. 16, and then $12.24m on Jul. 17. **Classification: constructive base.** No project catalyst is primary-source-verified for the interval. [Gate spot candles](https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair=AKE_USDT&interval=1d&from=1783900800&to=1784851200)
* **Aug. 13-14, 2026: $0.0040667 open to $0.0162681 intraday high.** This is the report's original acceleration: the Aug. 3-12 consolidation was followed by $18.95m and $34.64m spot quote volume. **Classification: constructive base.** The reported community and node narratives remain **unverified catalyst hypotheses**, not labels. [Gate spot candles](https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair=AKE_USDT&interval=1d&from=1786233600&to=1786838400)

### Falsifiable cross-case observations

1. **The repeatable observation is turnover expansion after a bounded state, not a named catalyst.** VELVET Jun./Aug., LAB Apr., and AKE Apr./Jul./Aug. all left comparatively narrow price regions with a sharp Gate quote-volume increase. Test a pre-specified 7-day range percentile plus current-volume/trailing-median-volume rule against the full listed-pair universe. A failure is a non-positive net 1d, 3d, or 7d return versus matched controls after fees and slippage.
2. **A base is not sufficient for continuation.** BEAT's Jul.-Aug. expansion and LAB's May burst produced large turnover but violent reversals; their pre-move regimes were already active or unstable. Test whether excluding assets with an elevated preceding 7-day realized range, rather than merely requiring a breakout, reduces maximum adverse excursion without removing all positive expectancy.
3. **A first-day/new-market move must be segmented from base breakouts.** LAB's October episode had no observable pre-history on Gate. Test a minimum 30 completed UTC days of pair history and reject signals that fail it; if post-listing events outperform, report them as a separate event-driven cohort rather than evidence for a base signal.
4. **Official event timing is still not causal evidence.** Velvet's Jun. 2 official announcement predates the Jun. 3-11 run, while no equivalently dated primary catalyst was found for the other selected episodes. Test announcements only when their publication timestamp precedes the signal cutoff and compare event-adjacent episodes with structurally matched non-event episodes. Do not upgrade a temporal match to a causal label without independent, timestamped evidence.

The evidence base remains venue-specific: all price and volume figures in this section are Gate `*_USDT` daily spot OHLCV, and volume is Gate quote volume in USDT. No claim here establishes aggregate market volume, buyer identity, leverage positioning, or the cause of any move.
