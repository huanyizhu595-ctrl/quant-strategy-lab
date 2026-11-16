"""Pure signal calculations for the Donchian strategy."""

import pandas as pd

REQUIRED_COLUMNS = {"High", "Low", "Close"}


def _validate_window(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def add_donchian_signals(
    prices: pd.DataFrame,
    entry_window: int = 20,
    exit_window: int = 10,
) -> pd.DataFrame:
    """Return a copy of prices with Donchian levels and intraday touch signals.

    The rolling levels are shifted by one row. Therefore, each day's signal
    uses only information that was available before that day began.
    """

    _validate_window("entry_window", entry_window)
    _validate_window("exit_window", exit_window)

    missing_columns = REQUIRED_COLUMNS.difference(prices.columns)
    if missing_columns:
        missing = ", ".join(sorted(missing_columns))
        raise ValueError(f"Missing required columns: {missing}")

    if prices.index.has_duplicates:
        raise ValueError("Price index contains duplicate dates")

    if not prices.index.is_monotonic_increasing:
        raise ValueError("Price index must be sorted in ascending order")

    result = prices.copy()
    result["EntryLevel"] = (
        result["High"]
        .rolling(window=entry_window, min_periods=entry_window)
        .max()
        .shift(1)
    )
    result["ExitLevel"] = (
        result["Low"]
        .rolling(window=exit_window, min_periods=exit_window)
        .min()
        .shift(1)
    )
    result["EntrySignal"] = result["High"] >= result["EntryLevel"]
    result["ExitSignal"] = result["Low"] <= result["ExitLevel"]

    return result
