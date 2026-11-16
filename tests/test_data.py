import pandas as pd
import pytest

from src.data import normalize_alpaca_bars, validate_prices


def make_alpaca_bars() -> pd.DataFrame:
    index = pd.MultiIndex.from_product(
        [
            ["SPY"],
            pd.to_datetime(
                ["2024-01-02T05:00:00Z", "2024-01-03T05:00:00Z"],
                utc=True,
            ),
        ],
        names=["symbol", "timestamp"],
    )
    return pd.DataFrame(
        {
            "open": [470.0, 472.0],
            "high": [473.0, 475.0],
            "low": [469.0, 471.0],
            "close": [472.0, 474.0],
            "volume": [80_000_000, 75_000_000],
            "trade_count": [500_000, 480_000],
            "vwap": [471.5, 473.2],
        },
        index=index,
    )


def test_normalize_alpaca_bars_returns_expected_ohlcv() -> None:
    prices = normalize_alpaca_bars(make_alpaca_bars())

    assert prices.columns.tolist() == ["Open", "High", "Low", "Close", "Volume"]
    assert prices.index.tolist() == [
        pd.Timestamp("2024-01-02"),
        pd.Timestamp("2024-01-03"),
    ]
    assert prices.loc["2024-01-02", "Close"] == 472.0


def test_normalize_alpaca_bars_rejects_empty_response() -> None:
    with pytest.raises(ValueError, match="no price data"):
        normalize_alpaca_bars(pd.DataFrame())


def test_validate_prices_rejects_inconsistent_high() -> None:
    prices = normalize_alpaca_bars(make_alpaca_bars())
    prices.loc[pd.Timestamp("2024-01-02"), "High"] = 460.0

    with pytest.raises(ValueError, match="High is below"):
        validate_prices(prices)
