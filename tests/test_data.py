import pandas as pd
import pytest

from src.data import TICKERS, normalize_twelve_data_response, validate_prices


def make_twelve_data_response() -> dict[str, object]:
    return {
        "meta": {"symbol": "NVDA", "interval": "1day"},
        "values": [
            {
                "datetime": "2024-01-03",
                "open": "472.0",
                "high": "475.0",
                "low": "471.0",
                "close": "474.0",
                "volume": "75000000",
            },
            {
                "datetime": "2024-01-02",
                "open": "470.0",
                "high": "473.0",
                "low": "469.0",
                "close": "472.0",
                "volume": "80000000",
            },
        ],
        "status": "ok",
    }


def test_normalize_twelve_data_returns_sorted_numeric_ohlcv() -> None:
    prices = normalize_twelve_data_response(make_twelve_data_response(), "NVDA")

    assert prices.columns.tolist() == ["Open", "High", "Low", "Close", "Volume"]
    assert prices.index.tolist() == [
        pd.Timestamp("2024-01-02"),
        pd.Timestamp("2024-01-03"),
    ]
    assert prices.loc["2024-01-02", "Close"] == 472.0
    assert prices["Volume"].dtype.kind in "fi"


def test_configured_tickers_are_nvda_mu_and_msft() -> None:
    assert TICKERS == ("NVDA", "MU", "MSFT")


def test_normalize_twelve_data_rejects_api_error() -> None:
    payload = {"status": "error", "code": 401, "message": "invalid api key"}

    with pytest.raises(RuntimeError, match="invalid api key"):
        normalize_twelve_data_response(payload, "NVDA")


def test_normalize_twelve_data_rejects_empty_response() -> None:
    with pytest.raises(ValueError, match="no price data"):
        normalize_twelve_data_response({"status": "ok", "values": []}, "NVDA")


def test_normalize_twelve_data_rejects_missing_fields() -> None:
    payload = {"status": "ok", "values": [{"datetime": "2024-01-02"}]}

    with pytest.raises(ValueError, match="missing datetime/OHLCV"):
        normalize_twelve_data_response(payload, "NVDA")


def test_validate_prices_rejects_inconsistent_high() -> None:
    prices = normalize_twelve_data_response(make_twelve_data_response(), "NVDA")
    prices.loc[pd.Timestamp("2024-01-02"), "High"] = 460.0

    with pytest.raises(ValueError, match="High is below"):
        validate_prices(prices)
