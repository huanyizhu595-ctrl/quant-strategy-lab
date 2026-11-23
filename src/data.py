"""Download and validate adjusted daily bars from Twelve Data."""

import json
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"
ENV_PATH = PROJECT_ROOT / ".env"

TWELVE_DATA_URL = "https://api.twelvedata.com/time_series"
TICKERS = ("NVDA", "MU", "MSFT")
START_DATE = date(2016, 1, 1)
END_DATE = date.today()
PRICE_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


def load_required_env(name: str) -> str:
    """Load one required value from the project-local, git-ignored .env."""

    load_dotenv(ENV_PATH)
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing {name} in the project .env file")
    return value


def validate_prices(prices: pd.DataFrame) -> None:
    """Reject incomplete or internally inconsistent OHLCV data."""

    missing_columns = [
        column for column in PRICE_COLUMNS if column not in prices.columns
    ]
    if missing_columns:
        raise ValueError(f"Missing required columns: {missing_columns}")

    if prices.empty:
        raise ValueError("Downloaded price data is empty")
    if prices.index.has_duplicates:
        raise ValueError("Price index contains duplicate dates")
    if not prices.index.is_monotonic_increasing:
        raise ValueError("Price index is not sorted in ascending order")

    missing_values = prices[PRICE_COLUMNS].isna().sum()
    if missing_values.any():
        raise ValueError(f"OHLCV contains missing values:\n{missing_values}")
    if (prices[["Open", "High", "Low", "Close"]] <= 0).any().any():
        raise ValueError("OHLC prices must be positive")
    if (prices["Volume"] < 0).any():
        raise ValueError("Volume cannot be negative")

    invalid_high = prices["High"] < prices[["Open", "Close"]].max(axis=1)
    invalid_low = prices["Low"] > prices[["Open", "Close"]].min(axis=1)
    if invalid_high.any():
        raise ValueError("High is below Open or Close in at least one row")
    if invalid_low.any():
        raise ValueError("Low is above Open or Close in at least one row")


def normalize_twelve_data_response(
    payload: dict[str, Any],
    ticker: str,
) -> pd.DataFrame:
    """Convert a Twelve Data time-series response into validated OHLCV."""

    if payload.get("status") == "error" or "code" in payload:
        message = payload.get("message", "unknown Twelve Data error")
        raise RuntimeError(f"Twelve Data error for {ticker}: {message}")

    values = payload.get("values")
    if not isinstance(values, list) or not values:
        raise ValueError(f"Twelve Data returned no price data for {ticker}")

    data = pd.DataFrame(values).rename(
        columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        }
    )
    missing = [column for column in PRICE_COLUMNS if column not in data.columns]
    if "datetime" not in data.columns or missing:
        raise ValueError(
            f"Twelve Data response for {ticker} is missing datetime/OHLCV fields"
        )

    data.index = pd.DatetimeIndex(
        pd.to_datetime(data.pop("datetime"), errors="raise"),
        name="Date",
    )
    prices = data[PRICE_COLUMNS].apply(pd.to_numeric, errors="raise")
    prices = prices.sort_index()
    validate_prices(prices)
    return prices


def fetch_twelve_data_prices(
    ticker: str,
    api_key: str,
    session: requests.Session | None = None,
) -> pd.DataFrame:
    """Fetch one complete adjusted daily history from Twelve Data."""

    http = session or requests.Session()
    response = http.get(
        TWELVE_DATA_URL,
        params={
            "symbol": ticker,
            "interval": "1day",
            "start_date": START_DATE.isoformat(),
            "end_date": END_DATE.isoformat(),
            "adjust": "all",
            "order": "asc",
            "timezone": "America/New_York",
            "format": "JSON",
            "apikey": api_key,
        },
        timeout=60,
    )
    response.raise_for_status()
    return normalize_twelve_data_response(response.json(), ticker)


def download_market_data() -> None:
    """Download, validate, and save adjusted daily snapshots for all tickers."""

    api_key = load_required_env("TWELVE_DATA_API_KEY")
    RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    downloaded_at_utc = datetime.now(UTC).isoformat()

    with requests.Session() as session:
        for ticker in TICKERS:
            prices = fetch_twelve_data_prices(ticker, api_key, session)
            prices_path = RAW_DATA_DIR / f"{ticker}_daily_adjusted.parquet"
            metadata_path = RAW_DATA_DIR / f"{ticker}_metadata.json"
            prices.to_parquet(prices_path)

            metadata = {
                "provider": "Twelve Data",
                "ticker": ticker,
                "interval": "1day",
                "adjustment": "all",
                "start_requested": START_DATE.isoformat(),
                "end_requested": END_DATE.isoformat(),
                "downloaded_at_utc": downloaded_at_utc,
                "first_date": prices.index.min().date().isoformat(),
                "last_date": prices.index.max().date().isoformat(),
                "rows": len(prices),
                "columns": prices.columns.tolist(),
                "missing_values": prices.isna().sum().to_dict(),
            }
            metadata_path.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            print(f"Download complete: {ticker}")
            print(
                f"Date range: {prices.index.min().date()} to "
                f"{prices.index.max().date()}"
            )
            print(f"Rows: {len(prices):,}")
            print(f"Adjusted prices: {prices_path}")
            print(f"Metadata: {metadata_path}")


if __name__ == "__main__":
    download_market_data()
