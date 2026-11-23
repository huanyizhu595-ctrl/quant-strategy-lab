"""Estimate portfolio risk and turn alpha scores into controlled weights."""

import json
from pathlib import Path

import pandas as pd

from src.data import TICKERS
from src.portfolio_risk import (
    PortfolioRiskConfig,
    build_close_frame,
    calculate_ewma_covariances,
    calculate_log_returns,
    calculate_risk_managed_weights,
    config_as_dict,
    covariance_to_correlation,
    historical_fixed_weight_stress,
)

PROJECT_ROOT = Path(__file__).resolve().parent
RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"
ALPHA_DIR = PROJECT_ROOT / "outputs" / "alpha"
RISK_DIR = PROJECT_ROOT / "outputs" / "risk"


def _load_alpha_scores() -> pd.DataFrame:
    scores: dict[str, pd.Series] = {}
    for ticker in TICKERS:
        path = ALPHA_DIR / f"{ticker}_alpha.csv"
        if not path.exists():
            raise FileNotFoundError(
                f"Missing {path}. Run `python run_alpha_research.py` first."
            )
        frame = pd.read_csv(path, index_col="Date", parse_dates=True)
        eligible = frame["TradeEligible"].astype(bool)
        scores[ticker] = frame["EventAdjustedAlpha"].where(eligible, 0.0)
    result = pd.DataFrame(scores).sort_index()
    result.index = pd.DatetimeIndex(result.index, name="Date")
    return result


def main() -> None:
    config = PortfolioRiskConfig()
    price_frames = {
        ticker: pd.read_parquet(
            RAW_DATA_DIR / f"{ticker}_daily_adjusted.parquet"
        )
        for ticker in TICKERS
    }
    closes = build_close_frame(price_frames)
    returns = calculate_log_returns(closes)
    covariance_history = calculate_ewma_covariances(returns, config)
    scores = _load_alpha_scores().reindex(closes.index).fillna(0.0)
    risk_history = calculate_risk_managed_weights(
        scores,
        covariance_history,
        config,
    )

    RISK_DIR.mkdir(parents=True, exist_ok=True)
    risk_history.to_csv(RISK_DIR / "portfolio_risk_history.csv")

    latest_date = risk_history.index[-1]
    latest_covariance = covariance_history[latest_date]
    latest_correlation = covariance_to_correlation(latest_covariance)
    latest_covariance.to_csv(RISK_DIR / "latest_covariance.csv")
    latest_correlation.to_csv(RISK_DIR / "latest_correlation.csv")

    latest = risk_history.loc[latest_date]
    weights = pd.Series(
        {ticker: float(latest[f"{ticker}Weight"]) for ticker in TICKERS}
    )
    report = {
        "as_of": latest_date.date().isoformat(),
        "execution_timing": "weights dated t are for execution no earlier than t+1",
        "configuration": config_as_dict(config),
        "alpha_scores": {
            ticker: float(latest[f"{ticker}Score"]) for ticker in TICKERS
        },
        "recommended_weights": weights.to_dict(),
        "cash_weight": float(latest["CashWeight"]),
        "gross_exposure": float(latest["GrossExposure"]),
        "forecast_annual_volatility": float(
            latest["ForecastAnnualVolatility"]
        ),
        "one_day_var_95": float(latest["OneDayVaR95"]),
        "one_day_expected_shortfall_95": float(
            latest["OneDayExpectedShortfall95"]
        ),
        "asset_annual_volatility": {
            ticker: float(latest[f"{ticker}AnnualVolatility"])
            for ticker in TICKERS
        },
        "risk_contribution": {
            ticker: float(latest[f"{ticker}RiskContribution"])
            for ticker in TICKERS
        },
        "correlation": latest_correlation.to_dict(),
        "historical_fixed_weight_stress": historical_fixed_weight_stress(
            returns.loc[:latest_date],
            weights,
        ),
    }
    report_path = RISK_DIR / "latest_risk_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Risk history: {RISK_DIR / 'portfolio_risk_history.csv'}")
    print(f"Latest covariance: {RISK_DIR / 'latest_covariance.csv'}")
    print(f"Latest correlation: {RISK_DIR / 'latest_correlation.csv'}")
    print(f"Latest report: {report_path}")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
