"""Run independent configured backtests for every ticker."""

import json
from pathlib import Path

import pandas as pd

from src.backtest import BacktestConfig, run_backtest, save_backtest_result
from src.data import TICKERS
from src.reporting import create_performance_report

PROJECT_ROOT = Path(__file__).resolve().parent
RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"
OUTPUT_DIR = PROJECT_ROOT / "outputs"


def main() -> None:
    comparisons: dict[str, object] = {}

    for ticker in TICKERS:
        prices_path = RAW_DATA_DIR / f"{ticker}_daily_adjusted.parquet"
        ticker_output_dir = OUTPUT_DIR / ticker
        prices = pd.read_parquet(prices_path)
        config = BacktestConfig()
        result = run_backtest(prices, config)
        save_backtest_result(result, ticker_output_dir)
        comparison, _ = create_performance_report(
            prices,
            result,
            config,
            ticker_output_dir,
            ticker,
        )
        comparisons[ticker] = comparison
        print(f"Completed: {ticker} -> {ticker_output_dir}")

    (OUTPUT_DIR / "multi_ticker_summary.json").write_text(
        json.dumps(comparisons, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(comparisons, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
