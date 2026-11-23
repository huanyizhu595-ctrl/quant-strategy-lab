"""Build and evaluate the mean-reversion alpha for every configured ticker."""

from pathlib import Path

import pandas as pd

from src.alpha import build_mean_reversion_alpha, evaluate_alpha
from src.data import TICKERS
from src.event_risk import (
    apply_event_risk_overlay,
    build_daily_filing_risk,
    build_daily_news_risk,
)
from src.filings import FILINGS_PATH
from src.news import NEWS_PATH

PROJECT_ROOT = Path(__file__).resolve().parent
RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"
OUTPUT_DIR = PROJECT_ROOT / "outputs" / "alpha"


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    combined_summaries: list[pd.DataFrame] = []
    news = pd.read_parquet(NEWS_PATH) if NEWS_PATH.exists() else None
    filings = pd.read_parquet(FILINGS_PATH) if FILINGS_PATH.exists() else None

    if news is None:
        print("Historical news not found; event overlay will use gap risk only.")
    if filings is None:
        print("SEC filings not found; run `python -m src.filings` to add them.")

    for ticker in TICKERS:
        prices_path = RAW_DATA_DIR / f"{ticker}_daily_adjusted.parquet"
        if not prices_path.exists():
            raise FileNotFoundError(
                f"Missing {prices_path}. Run `python -m src.data` first."
            )

        prices = pd.read_parquet(prices_path)
        alpha_frame = build_mean_reversion_alpha(prices)
        daily_news_risk = (
            build_daily_news_risk(news, prices.index, ticker)
            if news is not None
            else None
        )
        daily_filing_risk = (
            build_daily_filing_risk(filings, prices.index, ticker)
            if filings is not None
            else None
        )
        research_frame = apply_event_risk_overlay(
            alpha_frame,
            daily_news_risk=daily_news_risk,
            daily_filing_risk=daily_filing_risk,
        )

        baseline_summary = evaluate_alpha(
            research_frame,
            score_column="AlphaScore",
        ).reset_index()
        baseline_summary.insert(0, "Model", "PriceAlpha")
        event_summary = evaluate_alpha(
            research_frame,
            score_column="EventAdjustedAlpha",
        ).reset_index()
        event_summary.insert(0, "Model", "EventAdjustedAlpha")
        summary = pd.concat([baseline_summary, event_summary], ignore_index=True)

        alpha_path = OUTPUT_DIR / f"{ticker}_alpha.csv"
        summary_path = OUTPUT_DIR / f"{ticker}_alpha_summary.json"
        research_frame.to_csv(alpha_path)
        summary.to_json(
            summary_path,
            orient="records",
            indent=2,
            double_precision=10,
        )

        ticker_summary = summary.copy()
        ticker_summary.insert(0, "Ticker", ticker)
        combined_summaries.append(ticker_summary)

        print(f"Completed alpha research: {ticker}")
        print(f"Signal history: {alpha_path}")
        print(f"Diagnostics: {summary_path}")

    combined = pd.concat(combined_summaries, ignore_index=True)
    combined_path = OUTPUT_DIR / "multi_ticker_alpha_summary.csv"
    combined.to_csv(combined_path, index=False)
    print(f"Combined diagnostics: {combined_path}")


if __name__ == "__main__":
    main()
