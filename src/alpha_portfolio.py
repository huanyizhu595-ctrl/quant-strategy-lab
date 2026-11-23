"""Next-open portfolio backtests for daily risk-managed alpha weights."""

import math
from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class AlphaPortfolioBacktestConfig:
    """Execution and cost assumptions for the alpha portfolio."""

    initial_equity: float = 100_000.0
    one_way_cost: float = 0.0005
    annualization: int = 252

    def __post_init__(self) -> None:
        if not math.isfinite(self.initial_equity) or self.initial_equity <= 0:
            raise ValueError("initial_equity must be positive and finite")
        if not math.isfinite(self.one_way_cost) or not 0 <= self.one_way_cost < 1:
            raise ValueError("one_way_cost must be between 0 and 1")
        if (
            isinstance(self.annualization, bool)
            or not isinstance(self.annualization, int)
            or self.annualization <= 0
        ):
            raise ValueError("annualization must be a positive integer")


def extract_weight_frame(risk_history: pd.DataFrame, assets: list[str]) -> pd.DataFrame:
    """Extract ticker weights from the risk-model output."""

    columns = [f"{asset}Weight" for asset in assets]
    missing = [column for column in columns if column not in risk_history]
    if missing:
        raise ValueError(f"Missing weight columns: {missing}")
    weights = risk_history[columns].copy()
    weights.columns = assets
    return weights


def backtest_next_open_weights(
    open_prices: pd.DataFrame,
    signal_weights: pd.DataFrame,
    config: AlphaPortfolioBacktestConfig | None = None,
) -> pd.DataFrame:
    """Execute close-dated target weights at the following session's open.

    A target computed after close ``t`` becomes active at open ``t+1`` and
    earns the open-to-open return from ``t+1`` to ``t+2``. Turnover compares
    the new target with the prior holdings after they have drifted with prices.
    """

    config = config or AlphaPortfolioBacktestConfig()
    if open_prices.empty or signal_weights.empty:
        raise ValueError("Open prices and signal weights must be non-empty")
    if open_prices.index.has_duplicates or not open_prices.index.is_monotonic_increasing:
        raise ValueError("Open price index must be unique and sorted")
    if signal_weights.index.has_duplicates:
        raise ValueError("Signal weight index must be unique")
    if set(open_prices.columns) != set(signal_weights.columns):
        raise ValueError("Open prices and signal weights must contain the same assets")
    if open_prices.isna().any().any() or (open_prices <= 0).any().any():
        raise ValueError("Open prices must be positive and complete")

    assets = open_prices.columns.tolist()
    aligned_signals = signal_weights.reindex(open_prices.index).fillna(0.0)
    execution_weights = aligned_signals.shift(1).fillna(0.0)
    execution_weights = execution_weights.clip(lower=0.0)
    if (execution_weights.sum(axis=1) > 1.0 + 1e-12).any():
        raise ValueError("Target gross exposure cannot exceed 1")

    forward_returns = open_prices.shift(-1) / open_prices - 1.0
    rows: list[dict[str, float | pd.Timestamp]] = []
    equity = config.initial_equity
    pretrade_weights = pd.Series(0.0, index=assets)

    for position, date in enumerate(open_prices.index[:-1]):
        target = execution_weights.loc[date, assets].astype(float)
        asset_returns = forward_returns.loc[date, assets].astype(float)
        turnover = float((target - pretrade_weights).abs().sum())
        cost_dollars = equity * turnover * config.one_way_cost
        equity_after_cost = equity - cost_dollars
        gross_portfolio_return = float((target * asset_returns).sum())
        end_equity = equity_after_cost * (1.0 + gross_portfolio_return)
        net_return = end_equity / equity - 1.0

        output: dict[str, float | pd.Timestamp] = {
            "Date": date,
            "SignalDate": (
                open_prices.index[position - 1] if position > 0 else pd.NaT
            ),
            "GrossExposure": float(target.sum()),
            "CashWeight": float(1.0 - target.sum()),
            "Turnover": turnover,
            "TransactionCost": cost_dollars,
            "GrossReturn": gross_portfolio_return,
            "NetReturn": net_return,
            "Equity": end_equity,
        }
        for asset in assets:
            output[f"{asset}Weight"] = float(target.loc[asset])
        rows.append(output)

        denominator = 1.0 + gross_portfolio_return
        if denominator <= 0:
            raise RuntimeError("Portfolio lost 100% in one session")
        pretrade_weights = target * (1.0 + asset_returns) / denominator
        equity = end_equity

    result = pd.DataFrame(rows).set_index("Date")
    result.index = pd.DatetimeIndex(result.index, name="Date")
    result["RunningPeak"] = result["Equity"].cummax()
    result["Drawdown"] = result["Equity"] / result["RunningPeak"] - 1.0
    return result


def backtest_buy_and_hold(
    open_prices: pd.DataFrame,
    config: AlphaPortfolioBacktestConfig | None = None,
) -> pd.DataFrame:
    """Buy every asset equally at the first open and never rebalance."""

    config = config or AlphaPortfolioBacktestConfig()
    if len(open_prices) < 2:
        raise ValueError("At least two open-price rows are required")
    if open_prices.isna().any().any() or (open_prices <= 0).any().any():
        raise ValueError("Open prices must be positive and complete")

    assets = open_prices.columns.tolist()
    initial_weight = 1.0 / len(assets)
    initial_cost = config.initial_equity * config.one_way_cost
    invested_equity = config.initial_equity - initial_cost
    shares = pd.Series(
        {
            asset: invested_equity * initial_weight / open_prices.iloc[0][asset]
            for asset in assets
        }
    )
    previous_equity = config.initial_equity
    rows: list[dict[str, float | pd.Timestamp]] = []

    for position, date in enumerate(open_prices.index[:-1]):
        current_values = shares * open_prices.loc[date, assets]
        next_values = shares * open_prices.iloc[position + 1][assets]
        current_invested_equity = float(current_values.sum())
        next_equity = float(next_values.sum())
        net_return = next_equity / previous_equity - 1.0
        asset_returns = open_prices.iloc[position + 1][assets] / open_prices.loc[
            date, assets
        ] - 1.0
        current_weights = current_values / current_invested_equity

        output: dict[str, float | pd.Timestamp] = {
            "Date": date,
            "SignalDate": pd.NaT,
            "GrossExposure": 1.0,
            "CashWeight": 0.0,
            "Turnover": 1.0 if position == 0 else 0.0,
            "TransactionCost": initial_cost if position == 0 else 0.0,
            "GrossReturn": float((current_weights * asset_returns).sum()),
            "NetReturn": net_return,
            "Equity": next_equity,
        }
        for asset in assets:
            output[f"{asset}Weight"] = float(current_weights.loc[asset])
        rows.append(output)
        previous_equity = next_equity

    result = pd.DataFrame(rows).set_index("Date")
    result.index = pd.DatetimeIndex(result.index, name="Date")
    result["RunningPeak"] = result["Equity"].cummax()
    result["Drawdown"] = result["Equity"] / result["RunningPeak"] - 1.0
    return result


def summarize_backtest(
    history: pd.DataFrame,
    config: AlphaPortfolioBacktestConfig | None = None,
) -> dict[str, float | int | None]:
    """Summarize one already-sliced, continuous portfolio history."""

    config = config or AlphaPortfolioBacktestConfig()
    if history.empty:
        raise ValueError("Backtest history is empty")

    net_returns = history["NetReturn"]
    growth = float((1.0 + net_returns).prod())
    observations = len(history)
    years = observations / config.annualization
    cagr = growth ** (1.0 / years) - 1.0 if years > 0 else None
    annual_volatility = float(
        net_returns.std(ddof=1) * math.sqrt(config.annualization)
    )
    sharpe = (
        float(
            net_returns.mean()
            / net_returns.std(ddof=1)
            * math.sqrt(config.annualization)
        )
        if net_returns.std(ddof=1) > 0
        else None
    )
    period_equity = (1.0 + net_returns).cumprod()
    drawdown = period_equity / period_equity.cummax() - 1.0
    max_drawdown = float(drawdown.min())
    calmar = (
        float(cagr / abs(max_drawdown))
        if cagr is not None and max_drawdown < 0
        else None
    )
    return {
        "Observations": observations,
        "TotalReturn": growth - 1.0,
        "CAGR": cagr,
        "AnnualVolatility": annual_volatility,
        "Sharpe": sharpe,
        "MaxDrawdown": max_drawdown,
        "Calmar": calmar,
        "AverageGrossExposure": float(history["GrossExposure"].mean()),
        "AnnualizedTurnover": float(
            history["Turnover"].mean() * config.annualization
        ),
        "TransactionCosts": float(history["TransactionCost"].sum()),
    }
