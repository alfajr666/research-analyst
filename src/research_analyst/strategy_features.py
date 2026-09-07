"""Invocation-scoped Polars feature frames for strategy plugins."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import polars as pl

from polars_indicators import (
    rolling_rsi_series,
    rolling_stoch_rsi,
    wilder_atr_series,
    vwma_series,
)
from strategy_v2_context import ema_series, stoch_rsi, wilder_rsi


def build_feature_frame(
    bars: pl.DataFrame,
    *,
    ema: Mapping[str, int] | None = None,
    rsi: Mapping[str, int] | None = None,
    stoch: Mapping[str, tuple[int, int, int, int]] | None = None,
    rolling_stoch: Mapping[str, tuple[int, int, int, int]] | None = None,
    rolling_rsi: Mapping[str, int] | None = None,
    atr: Mapping[str, int] | None = None,
    bollinger: Mapping[str, tuple[int, float]] | None = None,
    vwma: Mapping[str, int] | None = None,
) -> pl.DataFrame:
    """Add requested numerical feature columns to one transient Polars frame."""
    if bars is None:
        return bars
    if bars.is_empty():
        return bars
    closes = bars["close"].cast(pl.Float64).to_list()
    columns: list[pl.Series] = []
    for name, length in (ema or {}).items():
        columns.append(pl.Series(name, ema_series(closes, length), dtype=pl.Float64))
    for name, length in (rsi or {}).items():
        columns.append(pl.Series(name, wilder_rsi(closes, length), dtype=pl.Float64))
    for name, parameters in (stoch or {}).items():
        raw, k, d = stoch_rsi(closes, *parameters)
        columns.extend((
            pl.Series(f"{name}_raw", raw, dtype=pl.Float64),
            pl.Series(f"{name}_k", k, dtype=pl.Float64),
            pl.Series(f"{name}_d", d, dtype=pl.Float64),
        ))
    for name, length in (rolling_rsi or {}).items():
        columns.append(rolling_rsi_series(closes, length).alias(name))
    for name, parameters in (rolling_stoch or {}).items():
        raw, k, d = rolling_stoch_rsi(closes, *parameters)
        columns.extend((
            raw.alias(f"{name}_raw"),
            k.alias(f"{name}_k"),
            d.alias(f"{name}_d"),
        ))
    for name, length in (atr or {}).items():
        columns.append(wilder_atr_series(bars, length).alias(name))
    for name, length in (vwma or {}).items():
        columns.append(vwma_series(bars, length).alias(name))
    for name, (length, deviations) in (bollinger or {}).items():
        middle = pl.col("close").cast(pl.Float64).rolling_mean(length, min_samples=length)
        spread = pl.col("close").cast(pl.Float64).rolling_std(
            length, ddof=0, min_samples=length,
        ) * deviations
        columns.extend((
            middle.alias(f"{name}_middle"),
            (middle - spread).alias(f"{name}_lower"),
            (middle + spread).alias(f"{name}_upper"),
            (2.0 * spread).alias(f"{name}_width"),
        ))
    return bars.with_columns(columns)


def cached_feature_frame(
    snapshot: dict[str, Any],
    key: str,
    bars: pl.DataFrame,
    builder: Callable[[pl.DataFrame], pl.DataFrame],
    *,
    asset: str | None = None,
    interval: str | None = None,
    cutoff: Any | None = None,
    feature_spec: dict[str, Any] | None = None,
) -> pl.DataFrame:
    """Cache one transient frame for the current plugin invocation only."""
    cache = snapshot.setdefault("_strategy_feature_cache", {})
    if key not in cache:
        if feature_spec is not None and asset is not None and interval is not None:
            from strategy_v2_context import get_shared_computation_context

            context = get_shared_computation_context()
            if context is not None:
                cache[key] = context.features(asset, interval, feature_spec, cutoff=cutoff)
            else:
                cache[key] = builder(bars)
        else:
            cache[key] = builder(bars)
    return cache[key]
