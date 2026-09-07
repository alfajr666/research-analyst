"""Shared ADX calculation used by current v2 strategies."""

from polars_indicators import dmi_adx
from strategy_v2_context import get_shared_computation_context


def dmi_adx_last(bars, length, smoothing, *, symbol=None, interval=None):
    context = get_shared_computation_context()
    if context is not None and symbol is not None and interval is not None:
        adx_series, plus_di, minus_di = context.dmi_adx(symbol, interval, length, smoothing)
        if not adx_series or plus_di is None or minus_di is None:
            return None
        return adx_series[-1], plus_di, minus_di
    adx_series, plus_di, minus_di = dmi_adx(bars, length, smoothing)
    if not adx_series or plus_di is None or minus_di is None:
        return None
    return adx_series[-1], plus_di, minus_di
