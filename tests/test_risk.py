import pandas as pd
import pytest

from src.risk import (
    PositionLot,
    calculate_atr,
    calculate_atr_position_size,
    calculate_initial_stop,
    calculate_next_add_price,
    calculate_pyramid_add_size,
    calculate_raised_stop,
    calculate_stop_risk,
    calculate_true_range,
)


def make_prices() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "High": [10.0, 12.0, 11.0, 10.0],
            "Low": [8.0, 11.0, 7.0, 8.0],
            "Close": [9.0, 11.5, 8.0, 9.0],
        },
        index=pd.date_range("2024-01-01", periods=4, freq="D"),
    )


def test_true_range_includes_overnight_gaps() -> None:
    true_range = calculate_true_range(make_prices())

    assert true_range.tolist() == pytest.approx([2.0, 3.0, 4.5, 2.0])


def test_wilder_atr_uses_sma_seed_then_smoothing() -> None:
    atr = calculate_atr(make_prices(), period=3)

    assert atr.iloc[:2].isna().all()
    assert atr.iloc[2] == pytest.approx((2.0 + 3.0 + 4.5) / 3)
    assert atr.iloc[3] == pytest.approx((atr.iloc[2] * 2 + 2.0) / 3)


def test_position_size_makes_one_atr_equal_one_percent_equity() -> None:
    shares = calculate_atr_position_size(
        equity=100_000,
        cash=100_000,
        entry_price=400,
        atr=5,
    )

    # Risk budget = 1,000; one N per share = 5, so one unit is 200 shares.
    assert shares == 200


def test_position_size_is_limited_by_cash_and_notional_cap() -> None:
    shares = calculate_atr_position_size(
        equity=100_000,
        cash=100_000,
        entry_price=400,
        atr=1,
    )

    # Turtle unit permits 1,000 shares, but 98,000 / 400 permits only 245.
    assert shares == 245


def test_position_size_uses_less_cash_after_losses() -> None:
    shares = calculate_atr_position_size(
        equity=80_000,
        cash=80_000,
        entry_price=400,
        atr=1,
    )

    # 98% of 80,000 permits 196 shares; no leverage is used.
    assert shares == 196


def test_initial_stop_is_two_atr_below_entry_by_default() -> None:
    stop = calculate_initial_stop(entry_price=400, atr=5)

    assert stop == 390


def test_invalid_risk_inputs_are_rejected() -> None:
    with pytest.raises(ValueError, match="risk_fraction"):
        calculate_atr_position_size(
            equity=100_000,
            cash=100_000,
            entry_price=400,
            atr=5,
            risk_fraction=1.1,
        )

    with pytest.raises(ValueError, match="period"):
        calculate_atr(make_prices(), period=0)


def test_next_add_is_half_atr_above_latest_fill() -> None:
    trigger = calculate_next_add_price(last_fill_price=400, atr=5)

    assert trigger == 402.5


def test_shared_stop_is_raised_but_never_lowered() -> None:
    raised = calculate_raised_stop(
        current_stop=390,
        latest_fill_price=402.5,
        atr=5,
    )
    unchanged = calculate_raised_stop(
        current_stop=395,
        latest_fill_price=402.5,
        atr=5,
    )

    assert raised == 392.5
    assert unchanged == 395


def test_stop_risk_combines_all_position_lots() -> None:
    lots = [
        PositionLot(entry_price=400, shares=100),
        PositionLot(entry_price=402.5, shares=100),
    ]

    risk = calculate_stop_risk(lots, stop_price=392.5)

    assert risk == 1_750


def test_pyramid_add_uses_full_planned_unit_when_cash_allows() -> None:
    shares = calculate_pyramid_add_size(
        lots=[PositionLot(entry_price=400, shares=100)],
        planned_unit_shares=100,
        cash=60_000,
        add_price=402.5,
    )

    assert shares == 100


def test_pyramid_add_has_no_non_classic_total_risk_cap() -> None:
    lots = [
        PositionLot(entry_price=400, shares=100),
        PositionLot(entry_price=402.5, shares=100),
        PositionLot(entry_price=405, shares=100),
    ]

    shares = calculate_pyramid_add_size(
        lots=lots,
        planned_unit_shares=100,
        cash=50_000,
        add_price=407.5,
        max_total_notional=200_000,
    )

    assert shares == 100


def test_pyramid_add_is_reduced_by_available_cash() -> None:
    shares = calculate_pyramid_add_size(
        lots=[PositionLot(entry_price=400, shares=10)],
        planned_unit_shares=100,
        cash=1_000,
        add_price=400,
    )

    assert shares == 2


def test_pyramid_add_stops_at_maximum_units() -> None:
    lots = [
        PositionLot(entry_price=400 + position * 2.5, shares=50)
        for position in range(4)
    ]

    shares = calculate_pyramid_add_size(
        lots=lots,
        planned_unit_shares=50,
        cash=20_000,
        add_price=410,
    )

    assert shares == 0


def test_pyramid_add_respects_total_notional_cap() -> None:
    shares = calculate_pyramid_add_size(
        lots=[PositionLot(entry_price=400, shares=200)],
        planned_unit_shares=100,
        cash=20_000,
        add_price=400,
    )

    # Existing notional is 80,000; only 18,000 remains under the 98,000 cap.
    assert shares == 45
