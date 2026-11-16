"""ATR-based position sizing, stops, and pyramid-add calculations."""

import math
from collections.abc import Sequence
from dataclasses import dataclass

import pandas as pd

REQUIRED_COLUMNS = {"High", "Low", "Close"}


@dataclass(frozen=True)
class PositionLot:
    """One filled unit in a pyramided long position."""

    entry_price: float
    shares: int

    def __post_init__(self) -> None:
        if not math.isfinite(self.entry_price) or self.entry_price <= 0:
            raise ValueError("entry_price must be a positive finite number")
        if isinstance(self.shares, bool) or not isinstance(self.shares, int):
            raise TypeError("shares must be an integer")
        if self.shares <= 0:
            raise ValueError("shares must be a positive integer")


def calculate_true_range(prices: pd.DataFrame) -> pd.Series:
    """Calculate daily True Range from High, Low, and previous Close."""

    missing_columns = REQUIRED_COLUMNS.difference(prices.columns)
    if missing_columns:
        missing = ", ".join(sorted(missing_columns))
        raise ValueError(f"Missing required columns: {missing}")

    previous_close = prices["Close"].shift(1)
    ranges = pd.concat(
        [
            prices["High"] - prices["Low"],
            (prices["High"] - previous_close).abs(),
            (prices["Low"] - previous_close).abs(),
        ],
        axis=1,
    )
    true_range = ranges.max(axis=1)
    true_range.name = "TrueRange"
    return true_range


def calculate_atr(prices: pd.DataFrame, period: int = 20) -> pd.Series:
    """Calculate Wilder's Average True Range with an exact SMA seed."""

    if isinstance(period, bool) or not isinstance(period, int) or period <= 0:
        raise ValueError("period must be a positive integer")

    true_range = calculate_true_range(prices)
    atr = pd.Series(float("nan"), index=prices.index, name="ATR")

    if len(true_range) < period:
        return atr

    first_atr_position = period - 1
    atr.iloc[first_atr_position] = true_range.iloc[:period].mean()

    for position in range(period, len(true_range)):
        previous_atr = atr.iloc[position - 1]
        current_true_range = true_range.iloc[position]
        atr.iloc[position] = (previous_atr * (period - 1) + current_true_range) / period

    return atr


def calculate_atr_position_size(
    *,
    equity: float,
    cash: float,
    entry_price: float,
    atr: float,
    risk_fraction: float = 0.01,
    max_notional: float = 98_000.0,
    cash_fraction_limit: float = 0.98,
) -> int:
    """Return a classic Turtle unit, capped by cash and notional limits.

    One N of adverse movement equals ``risk_fraction`` of account equity.
    The separate 2N initial stop therefore represents about 2% account risk
    when ``risk_fraction`` is 1%, before gaps and slippage.
    """

    positive_values = {
        "equity": equity,
        "cash": cash,
        "entry_price": entry_price,
        "atr": atr,
        "risk_fraction": risk_fraction,
        "max_notional": max_notional,
        "cash_fraction_limit": cash_fraction_limit,
    }
    for name, value in positive_values.items():
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be a positive finite number")

    if risk_fraction > 1:
        raise ValueError("risk_fraction cannot exceed 1")

    if cash_fraction_limit > 1:
        raise ValueError("cash_fraction_limit cannot exceed 1")

    risk_budget = equity * risk_fraction
    shares_by_risk = math.floor(risk_budget / atr)

    usable_cash = min(max_notional, cash * cash_fraction_limit)
    shares_by_cash = math.floor(usable_cash / entry_price)

    return max(0, min(shares_by_risk, shares_by_cash))


def calculate_initial_stop(
    entry_price: float,
    atr: float,
    stop_atr_multiple: float = 2.0,
) -> float:
    """Return a long position's initial stop price."""

    for name, value in {
        "entry_price": entry_price,
        "atr": atr,
        "stop_atr_multiple": stop_atr_multiple,
    }.items():
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be a positive finite number")

    return max(0.0, entry_price - atr * stop_atr_multiple)


def calculate_next_add_price(
    last_fill_price: float,
    atr: float,
    add_interval_atr: float = 0.5,
) -> float:
    """Return the next pyramid trigger above the most recent fill price."""

    for name, value in {
        "last_fill_price": last_fill_price,
        "atr": atr,
        "add_interval_atr": add_interval_atr,
    }.items():
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be a positive finite number")

    return last_fill_price + atr * add_interval_atr


def calculate_raised_stop(
    current_stop: float,
    latest_fill_price: float,
    atr: float,
    stop_atr_multiple: float = 2.0,
) -> float:
    """Raise a shared stop after an add without ever lowering it."""

    if not math.isfinite(current_stop) or current_stop < 0:
        raise ValueError("current_stop must be a non-negative finite number")

    candidate_stop = calculate_initial_stop(
        entry_price=latest_fill_price,
        atr=atr,
        stop_atr_multiple=stop_atr_multiple,
    )
    return max(current_stop, candidate_stop)


def calculate_stop_risk(
    lots: Sequence[PositionLot],
    stop_price: float,
) -> float:
    """Return total dollar loss if every lot exits at the shared stop."""

    if not math.isfinite(stop_price) or stop_price < 0:
        raise ValueError("stop_price must be a non-negative finite number")

    return sum(max(0.0, lot.entry_price - stop_price) * lot.shares for lot in lots)


def calculate_pyramid_add_size(
    *,
    lots: Sequence[PositionLot],
    planned_unit_shares: int,
    cash: float,
    add_price: float,
    max_units: int = 4,
    max_total_notional: float = 98_000.0,
    cash_fraction_limit: float = 0.98,
) -> int:
    """Return shares for one Turtle add under unit, cash, and notional caps."""

    if not lots:
        raise ValueError("at least one existing lot is required")

    if isinstance(planned_unit_shares, bool) or not isinstance(
        planned_unit_shares, int
    ):
        raise TypeError("planned_unit_shares must be an integer")
    if planned_unit_shares <= 0:
        raise ValueError("planned_unit_shares must be a positive integer")

    if isinstance(max_units, bool) or not isinstance(max_units, int):
        raise TypeError("max_units must be an integer")
    if max_units <= 0:
        raise ValueError("max_units must be a positive integer")

    if len(lots) >= max_units:
        return 0

    positive_values = {
        "cash": cash,
        "add_price": add_price,
        "max_total_notional": max_total_notional,
        "cash_fraction_limit": cash_fraction_limit,
    }
    for name, value in positive_values.items():
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be a positive finite number")

    if cash_fraction_limit > 1:
        raise ValueError("cash_fraction_limit cannot exceed 1")

    shares_by_cash = math.floor(cash * cash_fraction_limit / add_price)

    existing_shares = sum(lot.shares for lot in lots)
    remaining_notional = max(0.0, max_total_notional - existing_shares * add_price)
    shares_by_notional = math.floor(remaining_notional / add_price)

    return max(
        0,
        min(
            planned_unit_shares,
            shares_by_cash,
            shares_by_notional,
        ),
    )
