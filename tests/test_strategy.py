import pandas as pd
import pandas.testing as pdt
import pytest

from src.strategy import add_donchian_signals


def make_prices(
    highs: list[float],
    lows: list[float],
    closes: list[float],
) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=len(highs), freq="D")
    return pd.DataFrame(
        {"High": highs, "Low": lows, "Close": closes},
        index=index,
    )


def test_entry_level_excludes_current_bar() -> None:
    prices = make_prices(
        highs=[10, 11, 12, 20],
        lows=[8, 9, 10, 10],
        closes=[9, 10, 11, 11],
    )

    result = add_donchian_signals(prices, entry_window=3, exit_window=3)

    assert result.iloc[-1]["EntryLevel"] == 12
    assert bool(result.iloc[-1]["EntrySignal"])


def test_exit_level_excludes_current_bar() -> None:
    prices = make_prices(
        highs=[12, 11, 10, 10],
        lows=[10, 9, 8, 1],
        closes=[11, 10, 9, 9],
    )

    result = add_donchian_signals(prices, entry_window=3, exit_window=3)

    assert result.iloc[-1]["ExitLevel"] == 8
    assert bool(result.iloc[-1]["ExitSignal"])


def test_levels_are_unavailable_during_warmup() -> None:
    prices = make_prices(
        highs=[10, 11, 12, 13],
        lows=[8, 9, 10, 11],
        closes=[9, 10, 11, 12],
    )

    result = add_donchian_signals(prices, entry_window=3, exit_window=2)

    assert result["EntryLevel"].iloc[:3].isna().all()
    assert result["ExitLevel"].iloc[:2].isna().all()
    assert not result["EntrySignal"].iloc[:3].any()
    assert not result["ExitSignal"].iloc[:2].any()


def test_future_prices_do_not_change_past_signals() -> None:
    prices = make_prices(
        highs=[10, 11, 12, 13, 14, 15],
        lows=[8, 9, 10, 11, 12, 13],
        closes=[9, 10, 11, 12, 13, 14],
    )
    changed_future = prices.copy()
    changed_future.iloc[-1] = {"High": 1_000, "Low": 1, "Close": 500}

    original = add_donchian_signals(prices, entry_window=3, exit_window=2)
    changed = add_donchian_signals(changed_future, entry_window=3, exit_window=2)

    signal_columns = ["EntryLevel", "ExitLevel", "EntrySignal", "ExitSignal"]
    pdt.assert_frame_equal(
        original.iloc[:-1][signal_columns],
        changed.iloc[:-1][signal_columns],
    )


def test_input_frame_is_not_modified() -> None:
    prices = make_prices(
        highs=[10, 11, 12, 13],
        lows=[8, 9, 10, 11],
        closes=[9, 10, 11, 12],
    )
    original = prices.copy()

    add_donchian_signals(prices, entry_window=3, exit_window=2)

    pdt.assert_frame_equal(prices, original)


def test_invalid_input_is_rejected() -> None:
    prices = pd.DataFrame({"Close": [100.0]})

    with pytest.raises(ValueError, match="Missing required columns"):
        add_donchian_signals(prices)

    valid_prices = make_prices([10], [8], [9])
    with pytest.raises(ValueError, match="positive integer"):
        add_donchian_signals(valid_prices, entry_window=0)
