import pandas as pd
import pytest

from src.data import TICKERS, normalize_alpaca_bars, validate_prices


def make_alpaca_bars(ticker: str = "NVDA") -> pd.DataFrame:
    index = pd.MultiIndex.from_product(
        [
            [ticker],
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
    prices = normalize_alpaca_bars(make_alpaca_bars(), "NVDA")

    assert prices.columns.tolist() == ["Open", "High", "Low", "Close", "Volume"]
    assert prices.index.tolist() == [
        pd.Timestamp("2024-01-02"),
        pd.Timestamp("2024-01-03"),
    ]
    assert prices.loc["2024-01-02", "Close"] == 472.0


def test_configured_tickers_are_nvda_mu_and_msft() -> None:
    assert TICKERS == ("NVDA", "MU", "MSFT")


def test_normalize_alpaca_bars_selects_requested_ticker() -> None:
    nvda = make_alpaca_bars("NVDA")
    mu = make_alpaca_bars("MU")
    mu[["open", "high", "low", "close"]] += 100
    bars = pd.concat([nvda, mu])

    prices = normalize_alpaca_bars(bars, "MU")

    assert prices.loc["2024-01-02", "Close"] == 572.0


def test_normalize_alpaca_bars_rejects_missing_ticker() -> None:
    with pytest.raises(ValueError, match="does not contain MSFT"):
        normalize_alpaca_bars(make_alpaca_bars("NVDA"), "MSFT")


def test_normalize_alpaca_bars_rejects_empty_response() -> None:
    with pytest.raises(ValueError, match="no price data"):
        normalize_alpaca_bars(pd.DataFrame(), "NVDA")


def test_validate_prices_rejects_inconsistent_high() -> None:
    prices = normalize_alpaca_bars(make_alpaca_bars(), "NVDA")
    prices.loc[pd.Timestamp("2024-01-02"), "High"] = 460.0

    with pytest.raises(ValueError, match="High is below"):
        validate_prices(prices)
