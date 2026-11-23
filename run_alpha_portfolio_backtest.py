"""Compare price-only and event-adjusted risk-managed alpha portfolios."""

import json
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

from src.alpha_portfolio import (
    AlphaPortfolioBacktestConfig,
    backtest_buy_and_hold,
    backtest_next_open_weights,
    extract_weight_frame,
    summarize_backtest,
)
from src.data import TICKERS
from src.portfolio_risk import (
    PortfolioRiskConfig,
    build_close_frame,
    calculate_ewma_covariances,
    calculate_log_returns,
    calculate_risk_managed_weights,
)

PROJECT_ROOT = Path(__file__).resolve().parent
RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"
ALPHA_DIR = PROJECT_ROOT / "outputs" / "alpha"
OUTPUT_DIR = PROJECT_ROOT / "outputs" / "alpha_portfolio"

PERIODS = {
    "Research": ("2016-01-01", "2022-12-31"),
    "Validation": ("2023-01-01", "2024-12-31"),
    "Test": ("2025-01-01", None),
}


def _load_alpha_inputs() -> tuple[pd.DataFrame, pd.DataFrame]:
    price_scores: dict[str, pd.Series] = {}
    event_scores: dict[str, pd.Series] = {}
    for ticker in TICKERS:
        path = ALPHA_DIR / f"{ticker}_alpha.csv"
        frame = pd.read_csv(path, index_col="Date", parse_dates=True)
        price_scores[ticker] = frame["AlphaScore"].where(
            frame["AlphaActive"].astype(bool),
            0.0,
        )
        event_scores[ticker] = frame["EventAdjustedAlpha"].where(
            frame["TradeEligible"].astype(bool),
            0.0,
        )
    return (
        pd.DataFrame(price_scores).sort_index(),
        pd.DataFrame(event_scores).sort_index(),
    )


def _plot_test_period(histories: dict[str, pd.DataFrame]) -> None:
    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
    for name, history in histories.items():
        test = history.loc["2025-01-01":]
        normalized_equity = (1.0 + test["NetReturn"]).cumprod()
        drawdown = normalized_equity / normalized_equity.cummax() - 1.0
        axes[0].plot(normalized_equity.index, normalized_equity, label=name)
        axes[1].plot(drawdown.index, drawdown, label=name)
    axes[0].set_title("Out-of-sample equity growth (2025 onward)")
    axes[0].set_ylabel("Growth of $1")
    axes[0].legend()
    axes[0].grid(alpha=0.25)
    axes[1].set_title("Out-of-sample drawdown")
    axes[1].set_ylabel("Drawdown")
    axes[1].grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(OUTPUT_DIR / "test_equity_and_drawdown.png", dpi=160)
    plt.close(fig)


def main() -> None:
    risk_config = PortfolioRiskConfig()
    backtest_config = AlphaPortfolioBacktestConfig()
    price_frames = {
        ticker: pd.read_parquet(
            RAW_DATA_DIR / f"{ticker}_daily_adjusted.parquet"
        )
        for ticker in TICKERS
    }
    close_prices = build_close_frame(price_frames)
    open_prices = pd.DataFrame(
        {ticker: frame.loc[close_prices.index, "Open"] for ticker, frame in price_frames.items()}
    )
    returns = calculate_log_returns(close_prices)
    covariance_history = calculate_ewma_covariances(returns, risk_config)
    price_scores, event_scores = _load_alpha_inputs()

    model_scores = {
        "PriceAlphaRisk": price_scores.reindex(close_prices.index).fillna(0.0),
        "EventAdjustedRisk": event_scores.reindex(close_prices.index).fillna(0.0),
        "RiskOnlyNoAlpha": pd.DataFrame(
            1.0,
            index=close_prices.index,
            columns=list(TICKERS),
        ),
    }
    histories: dict[str, pd.DataFrame] = {}
    summaries: list[dict[str, object]] = []
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    for model, scores in model_scores.items():
        risk_history = calculate_risk_managed_weights(
            scores,
            covariance_history,
            risk_config,
        )
        weights = extract_weight_frame(risk_history, list(TICKERS))
        history = backtest_next_open_weights(
            open_prices,
            weights,
            backtest_config,
        )
        histories[model] = history
        history.to_csv(OUTPUT_DIR / f"{model}_history.csv")

        for period, (start, end) in PERIODS.items():
            sample = history.loc[start:end]
            summary: dict[str, object] = {
                "Model": model,
                "Period": period,
                "Start": sample.index.min().date().isoformat(),
                "End": sample.index.max().date().isoformat(),
            }
            summary.update(summarize_backtest(sample, backtest_config))
            summaries.append(summary)

    buy_hold_test: pd.DataFrame | None = None
    for period, (start, end) in PERIODS.items():
        period_opens = open_prices.loc[start:end]
        buy_hold = backtest_buy_and_hold(period_opens, backtest_config)
        if period == "Test":
            buy_hold_test = buy_hold
        summary = {
            "Model": "EqualWeightBuyHold",
            "Period": period,
            "Start": buy_hold.index.min().date().isoformat(),
            "End": buy_hold.index.max().date().isoformat(),
        }
        summary.update(summarize_backtest(buy_hold, backtest_config))
        summaries.append(summary)

    if buy_hold_test is None:
        raise RuntimeError("Test-period buy-and-hold history was not created")
    histories["EqualWeightBuyHold"] = buy_hold_test
    buy_hold_test.to_csv(OUTPUT_DIR / "EqualWeightBuyHold_test_history.csv")

    summary_frame = pd.DataFrame(summaries)
    summary_frame.to_csv(OUTPUT_DIR / "portfolio_backtest_summary.csv", index=False)
    (OUTPUT_DIR / "portfolio_backtest_summary.json").write_text(
        json.dumps(summaries, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _plot_test_period(histories)
    print(summary_frame.to_string(index=False))
    print(f"Results: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
