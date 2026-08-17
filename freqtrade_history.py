"""Read Freqtrade Feather futures history and identify distinct expansion episodes."""

from pathlib import Path

import pandas as pd

import config


BARS_PER_DAY = 96


def pair_name(path: Path) -> str:
    """Convert Freqtrade's filename convention into a canonical base asset."""
    return path.name.removesuffix("_USDT_USDT-15m-futures.feather")


def load_pair(path: Path) -> pd.DataFrame:
    """Load one 15-minute Freqtrade future and normalize its date order."""
    frame = pd.read_feather(path).sort_values("date").reset_index(drop=True)
    required = {"date", "open", "high", "low", "close", "volume"}
    if missing := required - set(frame.columns):
        raise ValueError(f"{path.name} is missing columns: {', '.join(sorted(missing))}")
    return frame


def expansion_episodes(frame: pd.DataFrame, asset: str, btc: pd.DataFrame, horizon_bars: int = 16,
                       minimum_move: float = 0.12, cooldown_bars: int = BARS_PER_DAY) -> list[dict]:
    """Find non-overlapping forward expansions using only the pre-event observation bar."""
    if len(frame) < BARS_PER_DAY * 10 + horizon_bars:
        return []

    future_highs = pd.concat(
        [frame["high"].shift(-offset) for offset in range(1, horizon_bars + 1)], axis=1
    ).max(axis=1)
    forward_mfe = future_highs / frame["close"] - 1.0
    candidates = frame.index[forward_mfe >= minimum_move].tolist()
    episodes = []
    next_allowed = 0
    btc_by_timestamp = btc.set_index("date")["close"]
    for index in candidates:
        if index < BARS_PER_DAY * 10 or index < next_allowed:
            continue
        observed = frame.iloc[index]
        base = frame.iloc[index - BARS_PER_DAY * 3:index]
        prior = frame.iloc[index - BARS_PER_DAY * 6:index - BARS_PER_DAY * 3]
        base_range = (base["high"].max() - base["low"].min()) / base["low"].min()
        prior_range = (prior["high"].max() - prior["low"].min()) / prior["low"].min()
        trend_return = observed["close"] / frame.iloc[index - BARS_PER_DAY * 4]["close"] - 1.0
        btc_then = btc_by_timestamp.get(frame.iloc[index - BARS_PER_DAY * 4]["date"])
        btc_now = btc_by_timestamp.get(observed["date"])
        btc_return = btc_now / btc_then - 1.0 if btc_then and btc_then > 0 else None
        episodes.append({
            "asset": asset,
            "observed_at": observed["date"],
            "close": float(observed["close"]),
            "forward_mfe": float(forward_mfe.iloc[index]),
            "trend_return_4d": float(trend_return),
            "relative_return_4d": float(trend_return - btc_return) if btc_return is not None else None,
            "base_range_3d": float(base_range),
            "base_to_prior_range": float(base_range / prior_range) if prior_range > 0 else None,
            "volume_ratio": float(observed["volume"] / frame.iloc[index - BARS_PER_DAY:index]["volume"].median()),
        })
        next_allowed = index + cooldown_bars
    return episodes


def build_expansion_corpus(data_dir: Path | None = None, horizon_bars: int = 16,
                           minimum_move: float = 0.12) -> pd.DataFrame:
    """Extract one or more independent explosive-move episodes per archived future."""
    directory = data_dir or Path(config.FREQTRADE_DATA_DIR)
    btc_path = directory / "BTC_USDT_USDT-15m-futures.feather"
    if not btc_path.exists():
        raise FileNotFoundError(f"BTC benchmark not found at {btc_path}")
    btc = load_pair(btc_path)
    episodes = []
    for path in sorted(directory.glob("*_USDT_USDT-15m-futures.feather")):
        if path == btc_path:
            continue
        episodes.extend(expansion_episodes(
            load_pair(path), pair_name(path), btc, horizon_bars=horizon_bars, minimum_move=minimum_move
        ))
    if not episodes:
        return pd.DataFrame()
    return pd.DataFrame(episodes).sort_values("forward_mfe", ascending=False).reset_index(drop=True)
