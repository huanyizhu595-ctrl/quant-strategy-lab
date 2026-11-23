import pandas as pd
import pandas.testing as pdt
import pytest

from src.alpha import (
    MeanReversionAlphaConfig,
    build_mean_reversion_alpha,
    evaluate_alpha,
)


def make_prices(
    closes: list[float],
    volumes: list[float] | None = None,
) -> pd.DataFrame:
    if volumes is None:
        volumes = [100 + index % 3 * 10 for index in range(len(closes))]
    index = pd.date_range("2024-01-01", periods=len(closes), freq="D")
    return pd.DataFrame(
        {
            "Close": closes,
            "Volume": volumes,
        },
        index=index,
    )


def small_config(**overrides: object) -> MeanReversionAlphaConfig:
    values: dict[str, object] = {
        "return_lookback": 2,
        "volatility_window": 3,
        "volume_window": 3,
        "trend_window": 5,
        "max_standardized_shock": 3.0,
        "volume_confirmation_weight": 0.5,
    }
    values.update(overrides)
    return MeanReversionAlphaConfig(**values)


def test_downside_shock_in_uptrend_creates_alpha() -> None:
    prices = make_prices(
        closes=[80, 82, 84, 86, 88, 90, 92, 94, 96, 98, 95],
        volumes=[90, 100, 110, 95, 105, 100, 90, 110, 95, 105, 1_000],
    )

    result = build_mean_reversion_alpha(prices, small_config())

    last = result.iloc[-1]
    assert bool(last["SignalReady"])
    assert bool(last["TrendRegime"])
    assert last["ReturnShockZ"] < 0
    assert last["VolumeShockZ"] > 0
    assert 0 < last["AlphaScore"] <= 1
    assert bool(last["AlphaActive"])


def test_long_term_downtrend_blocks_long_only_alpha() -> None:
    prices = make_prices(
        closes=[120, 118, 116, 114, 112, 110, 108, 106, 104, 102, 95],
        volumes=[90, 100, 110, 95, 105, 100, 90, 110, 95, 105, 1_000],
    )

    result = build_mean_reversion_alpha(prices, small_config())

    last = result.iloc[-1]
    assert bool(last["SignalReady"])
    assert not bool(last["TrendRegime"])
    assert last["AlphaRaw"] > 0
    assert last["AlphaScore"] == 0
    assert not bool(last["AlphaActive"])


def test_abnormal_volume_increases_but_does_not_create_alpha() -> None:
    closes = [80, 82, 84, 86, 88, 90, 92, 94, 96, 98, 95]
    base_volumes = [90, 100, 110, 95, 105, 100, 90, 110, 95, 105, 100]
    high_volumes = [*base_volumes[:-1], 1_000]

    base = build_mean_reversion_alpha(
        make_prices(closes, base_volumes),
        small_config(),
    )
    high = build_mean_reversion_alpha(
        make_prices(closes, high_volumes),
        small_config(),
    )

    assert high["AlphaScore"].iloc[-1] > base["AlphaScore"].iloc[-1]

    rising = make_prices(
        closes=[80, 82, 84, 86, 88, 90, 92, 94, 96, 98, 101],
        volumes=high_volumes,
    )
    rising_result = build_mean_reversion_alpha(rising, small_config())
    assert rising_result["VolumeShockZ"].iloc[-1] > 0
    assert rising_result["AlphaScore"].iloc[-1] == 0


def test_future_data_does_not_change_past_alpha() -> None:
    prices = make_prices(
        closes=[80, 82, 84, 86, 88, 90, 92, 94, 96, 98, 95, 97],
    )
    changed_future = prices.copy()
    changed_future.iloc[-1] = {"Close": 1_000, "Volume": 10_000}

    original = build_mean_reversion_alpha(prices, small_config())
    changed = build_mean_reversion_alpha(changed_future, small_config())

    alpha_columns = [
        "ReturnShockZ",
        "VolumeShockZ",
        "LaggedTrendMean",
        "AlphaScore",
        "SignalReady",
        "AlphaActive",
    ]
    pdt.assert_frame_equal(
        original.iloc[:-1][alpha_columns],
        changed.iloc[:-1][alpha_columns],
    )


def test_warmup_is_explicit_and_input_is_not_modified() -> None:
    prices = make_prices([80, 82, 84, 86, 88, 90, 92, 94])
    original = prices.copy()

    result = build_mean_reversion_alpha(prices, small_config())

    assert result.loc[~result["SignalReady"], "AlphaScore"].isna().all()
    assert result.loc[result["SignalReady"], "AlphaScore"].between(0, 1).all()
    pdt.assert_frame_equal(prices, original)


def test_invalid_config_and_prices_are_rejected() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        MeanReversionAlphaConfig(return_lookback=0)
    with pytest.raises(ValueError, match="cannot be negative"):
        MeanReversionAlphaConfig(volume_confirmation_weight=-0.1)

    with pytest.raises(ValueError, match="Missing required columns"):
        build_mean_reversion_alpha(pd.DataFrame({"Close": [100.0]}))

    invalid = make_prices([100.0, -1.0])
    with pytest.raises(ValueError, match="Close must be positive"):
        build_mean_reversion_alpha(invalid)


def test_evaluate_alpha_uses_only_available_forward_returns() -> None:
    alpha_frame = pd.DataFrame(
        {
            "Close": [100.0, 98.0, 99.0, 101.0, 102.0],
            "AlphaScore": [0.0, 0.8, 0.4, 0.0, 0.3],
        },
        index=pd.date_range("2024-01-01", periods=5, freq="D"),
    )

    summary = evaluate_alpha(alpha_frame, horizons=(1, 2))

    assert summary.loc[1, "Observations"] == 4
    assert summary.loc[1, "ActiveObservations"] == 2
    assert summary.loc[2, "Observations"] == 3
    assert summary.loc[1, "MeanForwardReturnActive"] > 0

    with pytest.raises(ValueError, match="positive integers"):
        evaluate_alpha(alpha_frame, horizons=(0,))
