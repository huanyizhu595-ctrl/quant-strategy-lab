import pandas as pd
import pytest

from src.backtest import BacktestConfig, BacktestResult
from src.reporting import (
    build_buy_hold_equity,
    build_yearly_performance,
    calculate_performance_stats,
)


def test_buy_hold_uses_same_integer_share_and_cash_constraints() -> None:
    prices = pd.DataFrame(
        {
            "Open": [10.0, 11.0],
            "Close": [10.0, 11.0],
        },
        index=pd.to_datetime(["2024-01-02", "2024-01-03"]),
    )
    config = BacktestConfig(
        initial_cash=1_000,
        max_total_notional=980,
        max_units=1,
        slippage_fraction=0,
    )

    result = build_buy_hold_equity(prices, config)

    assert result.shares == 98
    assert result.remaining_cash == 20
    assert result.equity.tolist() == [1_000, 1_098]


def test_performance_stats_calculate_drawdown() -> None:
    equity = pd.Series(
        [100.0, 120.0, 90.0, 130.0],
        index=pd.date_range("2024-01-01", periods=4, freq="D"),
    )

    stats = calculate_performance_stats(equity)

    assert stats["total_return"] == pytest.approx(0.30)
    assert stats["max_drawdown"] == pytest.approx(-0.25)


def test_performance_stats_can_use_pretrade_initial_equity() -> None:
    equity = pd.Series(
        [105.0, 110.0],
        index=pd.to_datetime(["2024-01-02", "2024-01-03"]),
    )

    stats = calculate_performance_stats(equity, initial_equity=100.0)

    assert stats["initial_equity"] == 100.0
    assert stats["total_return"] == pytest.approx(0.10)


def test_yearly_returns_chain_from_previous_year_end() -> None:
    dates = pd.to_datetime(["2023-01-02", "2023-12-29", "2024-01-02", "2024-12-31"])
    equity_curve = pd.DataFrame(
        {
            "Equity": [100.0, 110.0, 110.0, 121.0],
            "PositionShares": [0, 1, 1, 0],
        },
        index=dates,
    )
    fills = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2023-02-01", "2024-11-01"]),
            "Event": ["ENTRY", "DONCHIAN_EXIT"],
            "Side": ["BUY", "SELL"],
        }
    )
    result = BacktestResult(equity_curve=equity_curve, fills=fills, summary={})
    benchmark = pd.Series([100.0, 120.0, 120.0, 132.0], index=dates)

    yearly = build_yearly_performance(result, benchmark, initial_cash=100)

    assert yearly.loc[2023, "StrategyReturn"] == pytest.approx(0.10)
    assert yearly.loc[2024, "StrategyReturn"] == pytest.approx(0.10)
    assert yearly.loc[2023, "BuyHoldReturn"] == pytest.approx(0.20)
    assert yearly.loc[2024, "BuyHoldReturn"] == pytest.approx(0.10)
