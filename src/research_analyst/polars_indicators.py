"""Polars-native numerical kernels shared by regime and strategy paths."""

from __future__ import annotations

from typing import Any

import polars as pl


def _as_frame(bars: Any) -> pl.DataFrame:
    if isinstance(bars, pl.DataFrame):
        return bars
    if isinstance(bars, dict):
        return pl.DataFrame(bars, strict=False)
    if isinstance(bars, list):
        return pl.DataFrame(bars, strict=False)
    return pl.DataFrame()


def _seeded_rma(values: pl.Series, length: int) -> pl.Series:
    values = values.cast(pl.Float64)
    if length <= 0 or values.len() < length:
        return pl.Series("rma", [None] * values.len(), dtype=pl.Float64)
    seed = values.head(length).mean()
    seeded = pl.concat([
        pl.Series("values", [None] * (length - 1), dtype=pl.Float64),
        pl.Series("values", [seed], dtype=pl.Float64),
        values.slice(length),
    ])
    return seeded.ewm_mean(
        alpha=1.0 / length,
        adjust=False,
        min_samples=1,
    ).alias("rma")


def wilder_atr_series(bars: Any, length: int = 14) -> pl.Series:
    """Return an aligned Polars Wilder ATR series."""
    frame = _as_frame(bars)
    if length <= 0 or any(column not in frame.columns for column in ("high", "low", "close")):
        return pl.Series("atr", [None] * frame.height, dtype=pl.Float64)
    true_ranges = frame.select(
        pl.max_horizontal(
            (pl.col("high").cast(pl.Float64) - pl.col("low").cast(pl.Float64)),
            (pl.col("high").cast(pl.Float64) - pl.col("close").cast(pl.Float64).shift(1)).abs(),
            (pl.col("low").cast(pl.Float64) - pl.col("close").cast(pl.Float64).shift(1)).abs(),
        ).alias("true_range")
    )["true_range"]
    return _seeded_rma(true_ranges, length).alias("atr")


def dmi_adx_series(
    bars: Any,
    length: int,
    smoothing: int,
) -> tuple[list[float | None], float | None, float | None]:
    """Return an ADX series aligned to the input bars and final DI values."""
    frame = _as_frame(bars)
    if length <= 0 or smoothing <= 0 or any(
        column not in frame.columns for column in ("high", "low", "close")
    ):
        return [None] * frame.height, None, None
    if frame.height < length * 2 + smoothing + 1:
        return [None] * frame.height, None, None

    prices = frame.select(
        pl.col("high").cast(pl.Float64),
        pl.col("low").cast(pl.Float64),
        pl.col("close").cast(pl.Float64),
    ).with_columns(
        pl.max_horizontal(
            pl.col("high") - pl.col("low"),
            (pl.col("high") - pl.col("close").shift(1)).abs(),
            (pl.col("low") - pl.col("close").shift(1)).abs(),
        ).alias("true_range"),
        (pl.col("high") - pl.col("high").shift(1)).alias("up_move"),
        (pl.col("low").shift(1) - pl.col("low")).alias("down_move"),
    ).slice(1).with_columns(
        pl.when((pl.col("up_move") > pl.col("down_move")) & (pl.col("up_move") > 0))
        .then(pl.col("up_move"))
        .otherwise(0.0)
        .alias("plus_move"),
        pl.when((pl.col("down_move") > pl.col("up_move")) & (pl.col("down_move") > 0))
        .then(pl.col("down_move"))
        .otherwise(0.0)
        .alias("minus_move"),
    )

    dmi = pl.DataFrame({
        "index": pl.arange(0, prices.height, eager=True),
        "atr": _seeded_rma(prices["true_range"], length),
        "plus": _seeded_rma(prices["plus_move"], length),
        "minus": _seeded_rma(prices["minus_move"], length),
    }).filter(pl.col("index") >= length).with_columns(
        pl.when(pl.col("atr") != 0)
        .then(100.0 * pl.col("plus") / pl.col("atr"))
        .otherwise(0.0)
        .alias("plus_di"),
        pl.when(pl.col("atr") != 0)
        .then(100.0 * pl.col("minus") / pl.col("atr"))
        .otherwise(0.0)
        .alias("minus_di"),
    ).with_columns(
        pl.when((pl.col("plus_di") + pl.col("minus_di")) != 0)
        .then(100.0 * (pl.col("plus_di") - pl.col("minus_di")).abs() /
              (pl.col("plus_di") + pl.col("minus_di")))
        .otherwise(0.0)
        .alias("dx")
    )
    if dmi.is_empty() or dmi.height < smoothing:
        return [None] * frame.height, None, None
    adx = _seeded_rma(dmi["dx"], smoothing).to_list()
    aligned = pl.DataFrame({
        "index": pl.arange(0, frame.height, eager=True),
    }).join(
        pl.DataFrame({
            "index": dmi["index"] + 1,
            "adx": pl.Series("adx", adx, dtype=pl.Float64),
        }),
        on="index",
        how="left",
    ).sort("index")["adx"].to_list()
    return (
        aligned,
        float(dmi["plus_di"][-1]),
        float(dmi["minus_di"][-1]),
    )


def dmi_adx(
    bars: Any,
    length: int,
    smoothing: int,
) -> tuple[list[float], float | None, float | None]:
    """Return compact ADX values and final +DI/-DI values for compatibility."""
    series, plus_di, minus_di = dmi_adx_series(bars, length, smoothing)
    return [float(value) for value in series if value is not None], plus_di, minus_di


def rolling_rsi_series(values: Any, length: int = 14) -> pl.Series:
    """Return the legacy arithmetic rolling RSI, aligned to input values."""
    closes = pl.Series("close", values, dtype=pl.Float64)
    if length <= 0:
        return pl.Series("rsi", [None] * closes.len(), dtype=pl.Float64)
    return pl.DataFrame({"close": closes}).with_columns(
        pl.col("close").diff().alias("change"),
    ).with_columns(
        pl.when(pl.col("change").is_null()).then(pl.lit(None, dtype=pl.Float64))
        .when(pl.col("change") > 0).then(pl.col("change")).otherwise(0.0).alias("gain"),
        pl.when(pl.col("change").is_null()).then(pl.lit(None, dtype=pl.Float64))
        .when(pl.col("change") < 0).then(-pl.col("change")).otherwise(0.0).alias("loss"),
    ).with_columns(
        pl.col("gain").rolling_mean(length, min_samples=length).alias("avg_gain"),
        pl.col("loss").rolling_mean(length, min_samples=length).alias("avg_loss"),
    ).with_columns(
        pl.when(pl.col("avg_loss").is_null() | pl.col("avg_gain").is_null())
        .then(pl.lit(None, dtype=pl.Float64))
        .when(pl.col("avg_loss") == 0)
        .then(100.0)
        .otherwise(100.0 - 100.0 / (1.0 + pl.col("avg_gain") / pl.col("avg_loss")))
        .alias("rsi")
    )["rsi"]


def rolling_stoch_rsi(
    values: Any,
    rsi_length: int = 14,
    stoch_length: int = 14,
    k_smoothing: int = 3,
    d_smoothing: int = 3,
) -> tuple[pl.Series, pl.Series, pl.Series]:
    """Return StochRSI built from the legacy arithmetic rolling RSI."""
    rsi = rolling_rsi_series(values, rsi_length)
    frame = pl.DataFrame({"rsi": rsi}).with_columns(
        pl.col("rsi").rolling_min(stoch_length, min_samples=stoch_length).alias("low"),
        pl.col("rsi").rolling_max(stoch_length, min_samples=stoch_length).alias("high"),
    ).with_columns(
        pl.when(pl.col("low").is_null() | pl.col("high").is_null())
        .then(pl.lit(None, dtype=pl.Float64))
        .when(pl.col("high") == pl.col("low"))
        .then(0.0)
        .otherwise(100.0 * (pl.col("rsi") - pl.col("low")) /
                   (pl.col("high") - pl.col("low")))
        .alias("raw")
    ).with_columns(
        pl.col("raw").rolling_mean(k_smoothing, min_samples=k_smoothing).alias("k"),
    ).with_columns(
        pl.col("k").rolling_mean(d_smoothing, min_samples=d_smoothing).alias("d"),
    )
    return frame["raw"], frame["k"], frame["d"]


def vwma_last(bars: Any, length: int) -> float | None:
    """Return the final volume-weighted close over a complete rolling window."""
    frame = _as_frame(bars)
    if length <= 0 or frame.height < length or any(
        column not in frame.columns for column in ("close", "volume")
    ):
        return None
    result = frame.select(
        (pl.col("close").cast(pl.Float64) * pl.col("volume").cast(pl.Float64))
        .rolling_sum(length, min_samples=length)
        .alias("pv"),
        pl.col("volume").cast(pl.Float64)
        .rolling_sum(length, min_samples=length)
        .alias("volume"),
    ).tail(1).row(0)
    if result[0] is None or result[1] is None or result[1] <= 0:
        return None
    return float(result[0] / result[1])


def vwma_series(bars: Any, length: int) -> pl.Series:
    """Return an aligned rolling volume-weighted close series."""
    frame = _as_frame(bars)
    if length <= 0 or any(column not in frame.columns for column in ("close", "volume")):
        return pl.Series("vwma", [None] * frame.height, dtype=pl.Float64)
    return frame.select(
        (
            (pl.col("close").cast(pl.Float64) * pl.col("volume").cast(pl.Float64))
            .rolling_sum(length, min_samples=length)
            / pl.col("volume").cast(pl.Float64).rolling_sum(length, min_samples=length)
        ).alias("vwma")
    )["vwma"]


def realized_volatility(bars: Any, window: int) -> tuple[float | None, float | None]:
    """Return recent/prior squared-log-return volatility using Polars."""
    frame = _as_frame(bars)
    if window <= 0 or "close" not in frame.columns or frame.height < window * 2 + 1:
        return None, None
    returns = frame.select(
        pl.when((pl.col("close") > 0) & (pl.col("close").shift(1) > 0))
        .then((pl.col("close") / pl.col("close").shift(1)).log())
        .otherwise(None)
        .alias("return")
    )["return"].drop_nulls()
    if returns.len() < window * 2:
        return None, None
    prior = returns.slice(returns.len() - window * 2, window)
    recent = returns.tail(window)
    return (
        float((recent.pow(2).sum()) ** 0.5),
        float((prior.pow(2).sum()) ** 0.5),
    )


def strict_pivot_indices(
    bars: Any,
    left: int = 2,
    right: int = 2,
    *,
    strict: bool = True,
) -> tuple[list[int], list[int]]:
    """Return high/low fractal candidates using Polars rolling windows."""
    frame = _as_frame(bars)
    if left <= 0 or right <= 0 or frame.height < left + right + 1:
        return [], []
    indexed = frame.with_row_index("index").with_columns(
        pl.col("high").cast(pl.Float64).shift(1).rolling_max(
            left, min_samples=left,
        ).alias("left_high"),
        pl.col("high").cast(pl.Float64).shift(-right).rolling_max(
            right, min_samples=right,
        ).alias("right_high"),
        pl.col("low").cast(pl.Float64).shift(1).rolling_min(
            left, min_samples=left,
        ).alias("left_low"),
        pl.col("low").cast(pl.Float64).shift(-right).rolling_min(
            right, min_samples=right,
        ).alias("right_low"),
    )
    high_op = (lambda left, right: left > right) if strict else (lambda left, right: left >= right)
    low_op = (lambda left, right: left < right) if strict else (lambda left, right: left <= right)
    highs = indexed.filter(
        high_op(pl.col("high"), pl.col("left_high"))
        & high_op(pl.col("high"), pl.col("right_high"))
    )["index"].to_list()
    lows = indexed.filter(
        low_op(pl.col("low"), pl.col("left_low"))
        & low_op(pl.col("low"), pl.col("right_low"))
    )["index"].to_list()
    return [int(index) for index in highs], [int(index) for index in lows]


def ols_slope(values: Any) -> float | None:
    """Calculate a scalar OLS slope from Polars series arithmetic."""
    series = pl.Series("value", values, dtype=pl.Float64)
    if series.len() < 2 or not bool(series.is_finite().all()):
        return None
    x = pl.Series("index", range(series.len()), dtype=pl.Float64)
    x_mean = x.mean()
    y_mean = series.mean()
    denominator = ((x - x_mean) ** 2).sum()
    if denominator == 0:
        return None
    numerator = ((x - x_mean) * (series - y_mean)).sum()
    return float(numerator / denominator)
