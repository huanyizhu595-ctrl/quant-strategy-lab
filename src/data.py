"""Download and validate adjusted SPY daily bars from Alpaca."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"
ENV_PATH = PROJECT_ROOT / ".env"

TICKER = "SPY"
START_DATE = datetime(2016, 1, 1, tzinfo=UTC)
END_DATE = datetime(2026, 11, 14, tzinfo=UTC)
PRICE_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]


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

    invalid_high = prices["High"] < prices[["Open", "Close"]].max(axis=1)
    invalid_low = prices["Low"] > prices[["Open", "Close"]].min(axis=1)

    if invalid_high.any():
        raise ValueError("High is below Open or Close in at least one row")

    if invalid_low.any():
        raise ValueError("Low is above Open or Close in at least one row")


def normalize_alpaca_bars(bars: pd.DataFrame) -> pd.DataFrame:
    """Convert Alpaca's symbol/timestamp frame to local daily OHLCV data."""

    if bars is None or bars.empty:
        raise ValueError("Alpaca returned no price data")

    data = bars.copy()
    if isinstance(data.index, pd.MultiIndex):
        symbol_level = "symbol" if "symbol" in data.index.names else 0
        try:
            data = data.xs(TICKER, level=symbol_level)
        except KeyError as exc:
            raise ValueError(f"Alpaca response does not contain {TICKER}") from exc

    data = data.rename(
        columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        }
    )

    timestamps = pd.to_datetime(data.index, utc=True)
    new_york_dates = timestamps.tz_convert("America/New_York").date
    data.index = pd.DatetimeIndex(new_york_dates, name="Date")

    prices = data[PRICE_COLUMNS].copy()
    prices = prices.sort_index()
    prices = prices.loc[~prices.index.duplicated(keep="last")]
    validate_prices(prices)
    return prices


def load_alpaca_credentials() -> tuple[str, str]:
    """Load Alpaca credentials from the project-local, git-ignored .env."""

    load_dotenv(ENV_PATH)
    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")

    if not api_key or not secret_key:
        raise RuntimeError(
            "Missing Alpaca credentials. Fill ALPACA_API_KEY and "
            "ALPACA_SECRET_KEY in the project .env file."
        )

    return api_key, secret_key


def download_spy_data() -> None:
    """Download, validate, and save an adjusted SPY daily snapshot."""

    api_key, secret_key = load_alpaca_credentials()
    client = StockHistoricalDataClient(api_key, secret_key)
    request = StockBarsRequest(
        symbol_or_symbols=TICKER,
        timeframe=TimeFrame.Day,
        start=START_DATE,
        end=END_DATE,
        limit=10_000,
        adjustment=Adjustment.ALL,
        feed=DataFeed.SIP,
    )

    bars = client.get_stock_bars(request).df
    prices = normalize_alpaca_bars(bars)

    RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    prices_path = RAW_DATA_DIR / "SPY_daily_adjusted.parquet"
    metadata_path = RAW_DATA_DIR / "SPY_metadata.json"

    prices.to_parquet(prices_path)

    metadata = {
        "provider": "Alpaca Market Data API",
        "ticker": TICKER,
        "feed": DataFeed.SIP.value,
        "interval": "1Day",
        "adjustment": Adjustment.ALL.value,
        "start_requested": START_DATE.date().isoformat(),
        "end_requested": END_DATE.date().isoformat(),
        "downloaded_at_utc": datetime.now(UTC).isoformat(),
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

    print(f"Download complete: {TICKER}")
    print(f"Date range: {prices.index.min().date()} to {prices.index.max().date()}")
    print(f"Rows: {len(prices):,}")
    print(f"Adjusted prices: {prices_path}")
    print(f"Metadata: {metadata_path}")


if __name__ == "__main__":
    download_spy_data()
