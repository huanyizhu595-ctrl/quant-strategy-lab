import numpy as np
import pandas as pd
import pandas.testing as pdt
import pytest

from src.portfolio_risk import (
    PortfolioRiskConfig,
    calculate_ewma_covariances,
    calculate_risk_managed_weights,
    shrink_covariance,
)


def make_returns(periods: int = 100) -> pd.DataFrame:
    index = pd.date_range("2024-01-02", periods=periods, freq="B")
    base = np.linspace(-0.02, 0.02, periods)
    return pd.DataFrame(
        {
            "NVDA": base,
            "MU": base * 0.8 + 0.002,
            "MSFT": -base * 0.2 + 0.001,
        },
        index=index,
    )


def test_covariance_shrinkage_preserves_variance_and_reduces_correlation() -> None:
    covariance = pd.DataFrame(
        [[0.04, 0.03], [0.03, 0.09]],
        index=["A", "B"],
        columns=["A", "B"],
    )

    result = shrink_covariance(covariance, 0.25)

    assert np.diag(result).tolist() == pytest.approx([0.04, 0.09])
    assert result.loc["A", "B"] == pytest.approx(0.03 * 0.75)


def test_ewma_covariance_does_not_use_future_returns() -> None:
    config = PortfolioRiskConfig(min_observations=10)
    original = make_returns()
    changed = original.copy()
    cutoff = original.index[50]
    changed.loc[changed.index > cutoff, "NVDA"] = 1.0

    original_history = calculate_ewma_covariances(original, config)
    changed_history = calculate_ewma_covariances(changed, config)

    pdt.assert_frame_equal(original_history[cutoff], changed_history[cutoff])


def test_weights_respect_asset_cap_and_volatility_target() -> None:
    config = PortfolioRiskConfig(
        min_observations=10,
        target_annual_volatility=0.10,
        max_asset_weight=0.40,
    )
    returns = make_returns()
    history = calculate_ewma_covariances(returns, config)
    scores = pd.DataFrame(
        1.0,
        index=returns.index,
        columns=returns.columns,
    )

    result = calculate_risk_managed_weights(scores, history, config)
    latest = result.iloc[-1]

    assert latest["GrossExposure"] <= 1.0
    assert latest["ForecastAnnualVolatility"] <= 0.10 + 1e-12
    for ticker in returns.columns:
        assert 0 <= latest[f"{ticker}Weight"] <= 0.40
    contributions = sum(
        latest[f"{ticker}RiskContribution"] for ticker in returns.columns
    )
    assert contributions == pytest.approx(1.0)


def test_single_signal_keeps_cash_instead_of_breaking_asset_cap() -> None:
    config = PortfolioRiskConfig(min_observations=10, max_asset_weight=0.40)
    returns = make_returns()
    history = calculate_ewma_covariances(returns, config)
    scores = pd.DataFrame(0.0, index=returns.index, columns=returns.columns)
    scores["MSFT"] = 1.0

    result = calculate_risk_managed_weights(scores, history, config)
    latest = result.iloc[-1]

    assert latest["MSFTWeight"] <= 0.40
    assert latest["NVDAWeight"] == 0
    assert latest["MUWeight"] == 0
    assert latest["CashWeight"] >= 0.60


def test_zero_scores_produce_all_cash_and_zero_risk() -> None:
    config = PortfolioRiskConfig(min_observations=10)
    returns = make_returns()
    history = calculate_ewma_covariances(returns, config)
    scores = pd.DataFrame(0.0, index=returns.index, columns=returns.columns)

    result = calculate_risk_managed_weights(scores, history, config)
    latest = result.iloc[-1]

    assert latest["GrossExposure"] == 0
    assert latest["CashWeight"] == 1
    assert latest["ForecastAnnualVolatility"] == 0


def test_low_conviction_score_limits_gross_exposure() -> None:
    config = PortfolioRiskConfig(min_observations=10)
    returns = make_returns()
    history = calculate_ewma_covariances(returns, config)
    scores = pd.DataFrame(0.10, index=returns.index, columns=returns.columns)

    result = calculate_risk_managed_weights(scores, history, config)

    assert result.iloc[-1]["GrossExposure"] <= 0.10 + 1e-12
