import pandas as pd

from src.backtest import BacktestConfig, run_backtest


def make_prices(
    *,
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": opens,
            "High": highs,
            "Low": lows,
            "Close": closes,
            "Volume": [1_000_000] * len(opens),
        },
        index=pd.date_range("2024-01-01", periods=len(opens), freq="D"),
    )


def basic_config(**overrides: object) -> BacktestConfig:
    values: dict[str, object] = {
        "initial_cash": 10_000.0,
        "system1_entry_window": 2,
        "system1_exit_window": 2,
        "system2_entry_window": 55,
        "system2_exit_window": 20,
        "atr_period": 2,
        "risk_fraction": 0.01,
        "stop_atr_multiple": 10.0,
        "max_units": 1,
        "max_total_notional": 9_800.0,
        "slippage_fraction": 0.0,
    }
    values.update(overrides)
    return BacktestConfig(**values)


def test_intraday_entry_and_exit_fill_at_donchian_levels() -> None:
    prices = make_prices(
        opens=[10, 10, 10.8, 13, 12, 8],
        highs=[10, 11, 12.5, 13.5, 12.2, 8.5],
        lows=[9, 9.5, 10.5, 12.5, 8, 7.5],
        closes=[9.5, 10.5, 12.2, 13, 8.5, 8],
    )

    result = run_backtest(prices, basic_config())

    assert result.fills["Event"].tolist() == ["ENTRY", "DONCHIAN_EXIT"]
    assert result.fills["Date"].tolist() == [prices.index[2], prices.index[4]]
    assert result.fills["SignalDate"].tolist() == [prices.index[2], prices.index[4]]
    assert result.fills["ReferencePrice"].tolist() == [11.0, 10.5]
    assert result.summary["open_position_shares"] == 0


def test_gap_above_entry_level_fills_at_open() -> None:
    prices = make_prices(
        opens=[10, 10, 12],
        highs=[10, 11, 12.5],
        lows=[9, 9.5, 11.5],
        closes=[9.5, 10.5, 12.2],
    )

    result = run_backtest(prices, basic_config(system1_exit_window=10))

    entry = result.fills.iloc[0]
    assert entry["Event"] == "ENTRY"
    assert entry["Date"] == prices.index[2]
    assert entry["ReferencePrice"] == 12


def test_gap_below_stop_exits_at_open_before_any_add() -> None:
    prices = make_prices(
        opens=[10, 10, 12, 12.3, 9, 10],
        highs=[10, 11, 12.5, 12.5, 14, 11],
        lows=[9, 9.5, 11.5, 12, 8.5, 9],
        closes=[9.5, 10.5, 12.2, 12.4, 10, 10],
    )
    config = basic_config(
        stop_atr_multiple=2.0,
        max_units=4,
        system1_exit_window=4,
    )

    result = run_backtest(prices, config)

    assert "GAP_STOP" in result.fills["Event"].tolist()
    assert "ADD" not in result.fills["Event"].tolist()
    gap_stop = result.fills[result.fills["Event"] == "GAP_STOP"].iloc[0]
    assert gap_stop["ReferencePrice"] == 9


def test_multiple_half_atr_adds_can_occur_on_one_day() -> None:
    prices = make_prices(
        opens=[10, 10, 12, 13, 13.7, 14],
        highs=[10, 11, 12.5, 13.5, 20, 14.5],
        lows=[9, 9.5, 11.5, 12.5, 13, 13.5],
        closes=[9.5, 10.5, 12.2, 13, 13.8, 14],
    )
    config = basic_config(
        stop_atr_multiple=2.0,
        max_units=4,
        max_total_notional=9_800.0,
    )

    result = run_backtest(prices, config)

    adds_on_trigger_day = result.fills[
        (result.fills["Event"] == "ADD") & (result.fills["Date"] == prices.index[4])
    ]
    assert len(adds_on_trigger_day) == 2
    assert (result.equity_curve["Cash"] >= 0).all()


def test_stop_has_priority_when_stop_and_add_are_both_touched() -> None:
    prices = make_prices(
        opens=[10, 10, 12, 13, 13, 14],
        highs=[10, 11, 12.5, 13.5, 20, 14.5],
        lows=[9, 9.5, 11.5, 12.5, 8, 13.5],
        closes=[9.5, 10.5, 12.2, 13, 13, 14],
    )
    config = basic_config(
        stop_atr_multiple=2.0,
        max_units=4,
        system1_exit_window=4,
    )

    result = run_backtest(prices, config)

    day_events = result.fills[result.fills["Date"] == prices.index[4]]["Event"]
    assert day_events.tolist() == ["ATR_STOP"]


def test_entry_and_stop_touch_same_bar_uses_adverse_sequence() -> None:
    prices = make_prices(
        opens=[10, 10, 10.5, 10],
        highs=[10, 11, 12, 10.5],
        lows=[9, 9.5, 5, 9.5],
        closes=[9.5, 10.5, 11, 10],
    )
    config = basic_config(
        stop_atr_multiple=2.0,
        system1_exit_window=10,
    )

    result = run_backtest(prices, config)

    day_events = result.fills[result.fills["Date"] == prices.index[2]]["Event"]
    assert day_events.tolist() == ["ENTRY", "ATR_STOP"]


def test_add_and_raised_stop_touch_same_bar_uses_adverse_sequence() -> None:
    prices = make_prices(
        opens=[10, 10, 12, 13],
        highs=[10, 11, 12.5, 14],
        lows=[9, 9.5, 11.5, 10],
        closes=[9.5, 10.5, 12.2, 12],
    )
    config = basic_config(
        stop_atr_multiple=2.0,
        max_units=4,
        system1_exit_window=4,
    )

    result = run_backtest(prices, config)

    day_events = result.fills[result.fills["Date"] == prices.index[3]]["Event"]
    assert day_events.tolist() == ["ADD", "ATR_STOP"]


def test_intraday_entry_sizes_from_prior_day_atr() -> None:
    prices = make_prices(
        opens=[10, 10, 10],
        highs=[10, 11, 30],
        lows=[9, 9.5, 10],
        closes=[9.5, 10.5, 20],
    )
    config = basic_config(
        stop_atr_multiple=2.0,
        system1_exit_window=10,
    )

    result = run_backtest(prices, config)

    entry = result.fills.iloc[0]
    assert entry["ReferencePrice"] == 11
    assert entry["N"] == 1.25
    assert entry["Shares"] == 80


def test_winning_system1_breakout_is_skipped_then_system2_failsafe_enters() -> None:
    prices = make_prices(
        opens=[9.5, 10, 11, 12.5, 12, 11, 10.8, 11.5, 12.5, 13, 12.5, 11.5],
        highs=[10, 11, 12, 14, 13, 12, 11.5, 12.5, 13, 13.5, 13, 12.5],
        lows=[9, 9.5, 11.5, 12, 11, 10.5, 10, 11, 12, 12.5, 12, 10.5],
        closes=[9.5, 10.5, 11.8, 13.5, 11.5, 11, 10.5, 12, 12.8, 13.2, 12.5, 11],
    )
    config = basic_config(
        system1_entry_window=2,
        system1_exit_window=2,
        system2_entry_window=5,
        system2_exit_window=3,
        max_units=1,
        stop_atr_multiple=2.0,
    )

    result = run_backtest(prices, config)

    entries = result.fills[result.fills["Event"] == "ENTRY"]
    assert entries["System"].tolist() == ["S1", "S2"]
    assert entries["Date"].tolist() == [prices.index[2], prices.index[9]]
    assert not (
        (entries["System"] == "S1") & (entries["Date"] == prices.index[7])
    ).any()
    system2_exit = result.fills[
        (result.fills["System"] == "S2") & (result.fills["Side"] == "SELL")
    ].iloc[0]
    assert system2_exit["Date"] == prices.index[11]
    assert result.summary["system1_skipped_breakouts"] >= 1
    assert result.summary["system2_entries"] == 1


def test_losing_system1_breakout_allows_next_system1_entry() -> None:
    prices = make_prices(
        opens=[9.5, 10, 11, 8, 8.5, 9.5],
        highs=[10, 11, 12, 8.5, 9, 10],
        lows=[9, 9.5, 11.5, 7, 8, 8.5],
        closes=[9.5, 10.5, 11.8, 8, 8.5, 9.8],
    )
    config = basic_config(
        system2_entry_window=55,
        max_units=1,
        stop_atr_multiple=2.0,
    )

    result = run_backtest(prices, config)

    entries = result.fills[result.fills["Event"] == "ENTRY"]
    assert entries["System"].tolist() == ["S1", "S1"]
    assert entries["Date"].tolist() == [prices.index[2], prices.index[5]]
    assert result.summary["virtual_system1_losers"] >= 1


def test_backtest_is_deterministic() -> None:
    prices = make_prices(
        opens=[10, 10, 12, 13, 12, 8],
        highs=[10, 11, 12.5, 13.5, 12.2, 8.5],
        lows=[9, 9.5, 11.5, 12.5, 8, 7.5],
        closes=[9.5, 10.5, 12.2, 13, 8.5, 8],
    )
    config = basic_config()

    first = run_backtest(prices, config)
    second = run_backtest(prices, config)

    pd.testing.assert_frame_equal(first.equity_curve, second.equity_curve)
    pd.testing.assert_frame_equal(first.fills, second.fills)
    assert first.summary == second.summary
