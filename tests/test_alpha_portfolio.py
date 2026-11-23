import pandas as pd
import pytest

from src.alpha_portfolio import (
    AlphaPortfolioBacktestConfig,
    backtest_buy_and_hold,
    backtest_next_open_weights,
    summarize_backtest,
)


def test_signal_is_executed_at_next_open_without_lookahead() -> None:
    dates = pd.date_range("2024-01-02", periods=4, freq="B")
    opens = pd.DataFrame({"A": [100.0, 200.0, 220.0, 242.0]}, index=dates)
    signals = pd.DataFrame({"A": [1.0, 0.0, 0.0, 0.0]}, index=dates)
    config = AlphaPortfolioBacktestConfig(one_way_cost=0.0)

    result = backtest_next_open_weights(opens, signals, config)

    assert result.loc[dates[0], "AWeight"] == 0
    assert result.loc[dates[1], "AWeight"] == 1
    assert result.loc[dates[1], "GrossReturn"] == pytest.approx(0.10)
    assert result.loc[dates[2], "AWeight"] == 0


def test_transaction_cost_is_charged_on_one_way_turnover() -> None:
    dates = pd.date_range("2024-01-02", periods=3, freq="B")
    opens = pd.DataFrame({"A": [100.0, 100.0, 100.0]}, index=dates)
    signals = pd.DataFrame({"A": [0.5, 0.0, 0.0]}, index=dates)
    config = AlphaPortfolioBacktestConfig(
        initial_equity=100_000,
        one_way_cost=0.001,
    )

    result = backtest_next_open_weights(opens, signals, config)

    entry = result.loc[dates[1]]
    assert entry["Turnover"] == pytest.approx(0.5)
    assert entry["TransactionCost"] == pytest.approx(50.0)
    assert entry["Equity"] == pytest.approx(99_950.0)


def test_summary_recalculates_drawdown_within_requested_period() -> None:
    history = pd.DataFrame(
        {
            "NetReturn": [0.10, -0.20, 0.05],
            "GrossExposure": [0.5, 0.5, 0.5],
            "Turnover": [0.5, 0.0, 0.0],
            "TransactionCost": [25.0, 0.0, 0.0],
        },
        index=pd.date_range("2024-01-02", periods=3, freq="B"),
    )

    summary = summarize_backtest(history)

    assert summary["TotalReturn"] == pytest.approx(1.10 * 0.80 * 1.05 - 1)
    assert summary["MaxDrawdown"] == pytest.approx(-0.20)


def test_buy_and_hold_pays_once_and_does_not_rebalance() -> None:
    dates = pd.date_range("2024-01-02", periods=3, freq="B")
    opens = pd.DataFrame(
        {"A": [100.0, 110.0, 121.0], "B": [100.0, 100.0, 100.0]},
        index=dates,
    )
    config = AlphaPortfolioBacktestConfig(
        initial_equity=100_000,
        one_way_cost=0.001,
    )

    result = backtest_buy_and_hold(opens, config)

    assert result.iloc[0]["Turnover"] == 1
    assert result.iloc[0]["TransactionCost"] == pytest.approx(100.0)
    assert result.iloc[1]["Turnover"] == 0
    assert result.iloc[1]["AWeight"] > 0.5
