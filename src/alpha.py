"""Theory-driven alpha signals built from adjusted daily OHLCV data."""

import math
from collections.abc import Iterable
from dataclasses import dataclass

import pandas as pd

REQUIRED_ALPHA_COLUMNS = {"Close", "Volume"}


@dataclass(frozen=True)
class MeanReversionAlphaConfig:
    """Configuration for a long-only, short-horizon reversal alpha.

    The signal is intended to be calculated after the daily close and acted on
    no earlier than the next session. Rolling reference statistics are shifted
    by one row, so the current observation cannot change its own benchmark.
    """

    return_lookback: int = 5
    volatility_window: int = 20
    volume_window: int = 20
    trend_window: int = 200
    max_standardized_shock: float = 3.0
    volume_confirmation_weight: float = 0.5

    def __post_init__(self) -> None:
        integer_fields = {
            "return_lookback": self.return_lookback,
            "volatility_window": self.volatility_window,
            "volume_window": self.volume_window,
            "trend_window": self.trend_window,
        }
        for name, value in integer_fields.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")

        if not math.isfinite(self.max_standardized_shock):
            raise ValueError("max_standardized_shock must be finite")
        if self.max_standardized_shock <= 0:
            raise ValueError("max_standardized_shock must be positive")

        if not math.isfinite(self.volume_confirmation_weight):
            raise ValueError("volume_confirmation_weight must be finite")
        if self.volume_confirmation_weight < 0:
            raise ValueError("volume_confirmation_weight cannot be negative")


def _validate_alpha_prices(prices: pd.DataFrame) -> None:
    missing_columns = REQUIRED_ALPHA_COLUMNS.difference(prices.columns)
    if missing_columns:
        missing = ", ".join(sorted(missing_columns))
        raise ValueError(f"Missing required columns: {missing}")

    if prices.empty:
        raise ValueError("Price data is empty")

    if prices.index.has_duplicates:
        raise ValueError("Price index contains duplicate dates")

    if not prices.index.is_monotonic_increasing:
        raise ValueError("Price index must be sorted in ascending order")

    if prices[["Close", "Volume"]].isna().any().any():
        raise ValueError("Close and Volume cannot contain missing values")

    if prices[["Close", "Volume"]].isin([math.inf, -math.inf]).any().any():
        raise ValueError("Close and Volume must be finite")

    if (prices["Close"] <= 0).any():
        raise ValueError("Close must be positive")

    if (prices["Volume"] < 0).any():
        raise ValueError("Volume cannot be negative")


def build_mean_reversion_alpha(
    prices: pd.DataFrame,
    config: MeanReversionAlphaConfig | None = None,
) -> pd.DataFrame:
    """Add a transparent short-term mean-reversion alpha to daily prices.

    Model components:

    * ``ReturnShockZ``: the trailing return divided by lagged daily volatility,
      scaled to the same horizon. A negative value is a downside price shock.
    * ``VolumeShockZ``: current log-volume versus its lagged rolling history.
      Positive abnormal volume strengthens, but never creates, an alpha signal.
    * ``TrendRegime``: the close must remain above the lagged long-term mean.
      This prevents a short-horizon pullback model from blindly buying every
      decline in an established long-term downtrend.
    * ``AlphaScore``: a long-only score from 0 to 1. It is missing during the
      warmup period, zero when there is no eligible pullback, and positive when
      the model detects an eligible downside shock.

    The model uses current closing price and volume. It is therefore valid only
    after the bar is complete and must not be executed at that same close.
    """

    _validate_alpha_prices(prices)
    config = config or MeanReversionAlphaConfig()

    result = prices.copy()
    close = result["Close"].astype(float)
    volume = result["Volume"].astype(float)

    daily_return = close.pct_change(fill_method=None)
    short_return = close.pct_change(
        periods=config.return_lookback,
        fill_method=None,
    )

    lagged_daily_volatility = (
        daily_return.rolling(
            window=config.volatility_window,
            min_periods=config.volatility_window,
        )
        .std(ddof=0)
        .shift(1)
    )
    horizon_volatility = lagged_daily_volatility * math.sqrt(
        config.return_lookback
    )
    return_shock_z = short_return / horizon_volatility.replace(0.0, pd.NA)

    log_volume = volume.map(math.log1p)
    lagged_volume_mean = (
        log_volume.rolling(
            window=config.volume_window,
            min_periods=config.volume_window,
        )
        .mean()
        .shift(1)
    )
    lagged_volume_std = (
        log_volume.rolling(
            window=config.volume_window,
            min_periods=config.volume_window,
        )
        .std(ddof=0)
        .shift(1)
    )
    volume_shock_z = (log_volume - lagged_volume_mean) / lagged_volume_std.replace(
        0.0,
        pd.NA,
    )

    lagged_trend_mean = (
        close.rolling(
            window=config.trend_window,
            min_periods=config.trend_window,
        )
        .mean()
        .shift(1)
    )
    trend_regime = close > lagged_trend_mean

    clipped_reversal = (-return_shock_z).clip(
        lower=0.0,
        upper=config.max_standardized_shock,
    )
    volume_confirmation = (
        volume_shock_z.clip(
            lower=0.0,
            upper=config.max_standardized_shock,
        )
        / config.max_standardized_shock
    )
    alpha_raw = clipped_reversal * (
        1.0 + config.volume_confirmation_weight * volume_confirmation
    )
    maximum_raw_alpha = config.max_standardized_shock * (
        1.0 + config.volume_confirmation_weight
    )
    alpha_score = (alpha_raw / maximum_raw_alpha).clip(lower=0.0, upper=1.0)

    signal_ready = (
        return_shock_z.notna()
        & volume_shock_z.notna()
        & lagged_trend_mean.notna()
    )
    alpha_score = alpha_score.where(signal_ready)
    alpha_score = alpha_score.mask(signal_ready & ~trend_regime, 0.0)

    result["DailyReturn"] = daily_return
    result["ShortReturn"] = short_return
    result["LaggedDailyVolatility"] = lagged_daily_volatility
    result["ReturnShockZ"] = return_shock_z
    result["VolumeShockZ"] = volume_shock_z
    result["LaggedTrendMean"] = lagged_trend_mean
    result["TrendRegime"] = trend_regime.where(lagged_trend_mean.notna())
    result["AlphaRaw"] = alpha_raw.where(signal_ready)
    result["AlphaScore"] = alpha_score
    result["SignalReady"] = signal_ready
    result["AlphaActive"] = signal_ready & trend_regime & alpha_score.gt(0.0)

    return result


def evaluate_alpha(
    alpha_frame: pd.DataFrame,
    horizons: Iterable[int] = (1, 5, 10),
    score_column: str = "AlphaScore",
) -> pd.DataFrame:
    """Evaluate a continuous alpha score against subsequent close returns.

    This is a research diagnostic, not a tradable P&L calculation. Forward
    returns deliberately use future data and are kept in this separate function
    so they cannot accidentally enter the signal construction path.
    """

    required = {"Close", score_column}
    missing = required.difference(alpha_frame.columns)
    if missing:
        missing_names = ", ".join(sorted(missing))
        raise ValueError(f"Missing required columns: {missing_names}")

    horizon_values = list(horizons)
    for horizon in horizon_values:
        if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon <= 0:
            raise ValueError("horizons must contain positive integers")

    rows: list[dict[str, float | int]] = []
    score = alpha_frame[score_column]
    close = alpha_frame["Close"].astype(float)

    for horizon in horizon_values:
        forward_return = close.shift(-horizon) / close - 1.0
        valid = score.notna() & forward_return.notna()
        valid_score = score.loc[valid]
        valid_forward_return = forward_return.loc[valid]
        active = valid_score > 0.0

        pearson_ic = valid_score.corr(valid_forward_return, method="pearson")
        spearman_ic = valid_score.rank().corr(valid_forward_return.rank())

        rows.append(
            {
                "HorizonDays": horizon,
                "Observations": int(valid.sum()),
                "ActiveObservations": int(active.sum()),
                "PearsonIC": float(pearson_ic),
                "SpearmanIC": float(spearman_ic),
                "MeanForwardReturnAll": float(valid_forward_return.mean()),
                "MeanForwardReturnActive": float(
                    valid_forward_return.loc[active].mean()
                ),
                "ActiveHitRate": float(
                    valid_forward_return.loc[active].gt(0.0).mean()
                ),
            }
        )

    return pd.DataFrame(rows).set_index("HorizonDays")
