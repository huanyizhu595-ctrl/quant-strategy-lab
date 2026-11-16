"""Run the configured SPY backtest from the project root."""

import json
from pathlib import Path

import pandas as pd

from src.backtest import BacktestConfig, run_backtest, save_backtest_result
from src.reporting import create_performance_report

PROJECT_ROOT = Path(__file__).resolve().parent
PRICES_PATH = PROJECT_ROOT / "data" / "raw" / "SPY_daily_adjusted.parquet"
OUTPUT_DIR = PROJECT_ROOT / "outputs"


def main() -> None:
    prices = pd.read_parquet(PRICES_PATH)
    config = BacktestConfig()
    result = run_backtest(prices, config)
    save_backtest_result(result, OUTPUT_DIR)
    comparison, _ = create_performance_report(
        prices,
        result,
        config,
        OUTPUT_DIR,
    )
    print(json.dumps(comparison, ensure_ascii=False, indent=2))
    print(f"Outputs saved to: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
