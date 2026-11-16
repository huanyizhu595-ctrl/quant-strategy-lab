"""Benchmark analytics and static chart generation for backtest results."""

import json
import math
from dataclasses import dataclass
from pathlib import Path

import matplotlib
import pandas as pd
from matplotlib.ticker import FuncFormatter, PercentFormatter

from src.backtest import BacktestConfig, BacktestResult

matplotlib.use("Agg")
from matplotlib import pyplot as plt


@dataclass(frozen=True)
class BuyHoldResult:
    equity: pd.Series
    shares: int
    fill_price: float
    remaining_cash: float


def calculate_performance_stats(
    equity: pd.Series,
    initial_equity: float | None = None,
) -> dict[str, float | None]:
    """Calculate comparable performance statistics from an equity series."""

    if equity.empty:
        raise ValueError("Equity series is empty")
    if (equity <= 0).any():
        raise ValueError("Equity must remain positive")

    initial_value = (
        float(equity.iloc[0]) if initial_equity is None else float(initial_equity)
    )
    if initial_value <= 0:
        raise ValueError("Initial equity must be positive")

    performance_path = equity
    if initial_equity is not None:
        performance_path = pd.concat(
            [
                pd.Series(
                    [initial_value],
                    index=[equity.index[0] - pd.Timedelta(days=1)],
                ),
                equity,
            ]
        )

    final_equity = float(equity.iloc[-1])
    total_return = final_equity / initial_value - 1
    elapsed_years = max(
        (equity.index[-1] - equity.index[0]).days / 365.25,
        1 / 365.25,
    )
    cagr = (final_equity / initial_value) ** (1 / elapsed_years) - 1

    daily_returns = performance_path.pct_change().dropna()
    daily_std = float(daily_returns.std(ddof=1)) if len(daily_returns) > 1 else 0.0
    annual_volatility = daily_std * math.sqrt(252)
    sharpe = (
        float(daily_returns.mean() / daily_std * math.sqrt(252))
        if daily_std > 0
        else None
    )

    drawdown = performance_path / performance_path.cummax() - 1
    max_drawdown = float(drawdown.min())
    calmar = cagr / abs(max_drawdown) if max_drawdown < 0 else None

    return {
        "initial_equity": initial_value,
        "final_equity": final_equity,
        "total_return": total_return,
        "cagr": cagr,
        "annual_volatility": annual_volatility,
        "sharpe_zero_rate": sharpe,
        "max_drawdown": max_drawdown,
        "calmar": calmar,
    }


def build_buy_hold_equity(
    prices: pd.DataFrame,
    config: BacktestConfig,
) -> BuyHoldResult:
    """Buy SPY at the first open and hold using comparable cash constraints."""

    if prices.empty:
        raise ValueError("Price data is empty")

    reference_price = float(prices["Open"].iloc[0])
    fill_price = reference_price * (1 + config.slippage_fraction)
    investment_cap = min(
        config.max_total_notional,
        config.initial_cash * config.cash_fraction_limit,
    )
    shares = math.floor(investment_cap / fill_price)
    if shares <= 0:
        raise ValueError("Initial cash is insufficient for one benchmark share")

    remaining_cash = config.initial_cash - shares * fill_price
    equity = remaining_cash + shares * prices["Close"]
    equity.name = "BuyHoldEquity"
    return BuyHoldResult(
        equity=equity,
        shares=shares,
        fill_price=fill_price,
        remaining_cash=remaining_cash,
    )


def _year_drawdown(values: pd.Series, starting_equity: float) -> float:
    path = pd.concat(
        [
            pd.Series(
                [starting_equity], index=[values.index[0] - pd.Timedelta(days=1)]
            ),
            values,
        ]
    )
    return float((path / path.cummax() - 1).min())


def build_yearly_performance(
    result: BacktestResult,
    buy_hold_equity: pd.Series,
    initial_cash: float,
) -> pd.DataFrame:
    """Create calendar-year returns, drawdowns, exposure, and event counts."""

    equity_curve = result.equity_curve
    years = sorted(equity_curve.index.year.unique())
    rows: list[dict[str, object]] = []
    previous_strategy_equity = initial_cash
    previous_buy_hold_equity = initial_cash
    final_date = equity_curve.index[-1]

    for year in years:
        year_mask = equity_curve.index.year == year
        strategy_year = equity_curve.loc[year_mask, "Equity"]
        buy_hold_year = buy_hold_equity.loc[year_mask]
        fills_year = result.fills[result.fills["Date"].dt.year == year]

        strategy_end = float(strategy_year.iloc[-1])
        buy_hold_end = float(buy_hold_year.iloc[-1])
        rows.append(
            {
                "Year": int(year),
                "StrategyReturn": strategy_end / previous_strategy_equity - 1,
                "BuyHoldReturn": buy_hold_end / previous_buy_hold_equity - 1,
                "StrategyMaxDrawdown": _year_drawdown(
                    strategy_year, previous_strategy_equity
                ),
                "BuyHoldMaxDrawdown": _year_drawdown(
                    buy_hold_year, previous_buy_hold_equity
                ),
                "Exposure": float(
                    (equity_curve.loc[year_mask, "PositionShares"] > 0).mean()
                ),
                "Entries": int((fills_year["Event"] == "ENTRY").sum()),
                "Adds": int((fills_year["Event"] == "ADD").sum()),
                "Exits": int((fills_year["Side"] == "SELL").sum()),
                "IsPartialYear": bool(
                    year == final_date.year
                    and (final_date.month, final_date.day) != (12, 31)
                ),
            }
        )
        previous_strategy_equity = strategy_end
        previous_buy_hold_equity = buy_hold_end

    return pd.DataFrame(rows).set_index("Year")


def _save_performance_overview(
    equity_curve: pd.DataFrame,
    output_path: Path,
) -> None:
    strategy = equity_curve["Equity"]
    benchmark = equity_curve["BuyHoldEquity"]
    strategy_drawdown = strategy / strategy.cummax() - 1
    benchmark_drawdown = benchmark / benchmark.cummax() - 1
    strategy_color = "#1f77b4"
    benchmark_color = "#ff7f0e"

    figure, (equity_axis, drawdown_axis) = plt.subplots(
        2,
        1,
        figsize=(12, 8),
        sharex=True,
        gridspec_kw={"height_ratios": [2.2, 1]},
    )
    equity_axis.plot(
        strategy.index,
        strategy,
        label="Turtle S1/S2 + N",
        color=strategy_color,
        linewidth=2,
    )
    equity_axis.plot(
        benchmark.index,
        benchmark,
        label="SPY buy & hold",
        color=benchmark_color,
        linewidth=2,
    )
    equity_axis.set_title("Equity and drawdown comparison")
    equity_axis.set_ylabel("Portfolio value (USD)")
    equity_axis.yaxis.set_major_formatter(
        FuncFormatter(lambda value, _: f"${value:,.0f}")
    )
    equity_axis.grid(alpha=0.25)
    equity_axis.legend()

    drawdown_axis.fill_between(
        strategy_drawdown.index,
        strategy_drawdown,
        0,
        color=strategy_color,
        alpha=0.28,
        label="Turtle S1/S2 + N",
    )
    drawdown_axis.plot(
        benchmark_drawdown.index,
        benchmark_drawdown,
        color=benchmark_color,
        linewidth=1.5,
        label="SPY buy & hold",
    )
    drawdown_axis.axhline(0, color="0.4", linewidth=0.8)
    drawdown_axis.set_ylabel("Drawdown")
    drawdown_axis.set_xlabel("Date")
    drawdown_axis.yaxis.set_major_formatter(PercentFormatter(1.0))
    drawdown_axis.grid(alpha=0.25)
    drawdown_axis.legend()

    figure.tight_layout()
    figure.savefig(output_path, dpi=170, bbox_inches="tight")
    plt.close(figure)


def _save_yearly_returns(yearly: pd.DataFrame, output_path: Path) -> None:
    positions = list(range(len(yearly)))
    width = 0.38
    strategy_color = "#1f77b4"
    benchmark_color = "#ff7f0e"
    labels = [
        f"{year}{'*' if partial else ''}"
        for year, partial in zip(
            yearly.index,
            yearly["IsPartialYear"],
            strict=True,
        )
    ]

    figure, axis = plt.subplots(figsize=(12, 5.8))
    strategy_bars = axis.bar(
        [position - width / 2 for position in positions],
        yearly["StrategyReturn"],
        width,
        label="Turtle S1/S2 + N",
        color=strategy_color,
    )
    benchmark_bars = axis.bar(
        [position + width / 2 for position in positions],
        yearly["BuyHoldReturn"],
        width,
        label="SPY buy & hold",
        color=benchmark_color,
    )
    axis.axhline(0, color="0.35", linewidth=0.8)
    axis.set_title("Calendar-year returns")
    axis.set_ylabel("Return")
    axis.set_xticks(positions, labels)
    axis.yaxis.set_major_formatter(PercentFormatter(1.0))
    axis.grid(axis="y", alpha=0.25)
    axis.legend()

    for bars in (strategy_bars, benchmark_bars):
        for bar in bars:
            value = bar.get_height()
            vertical_alignment = "bottom" if value >= 0 else "top"
            offset = 3 if value >= 0 else -3
            axis.annotate(
                f"{value:.1%}",
                (bar.get_x() + bar.get_width() / 2, value),
                xytext=(0, offset),
                textcoords="offset points",
                ha="center",
                va=vertical_alignment,
                fontsize=8,
            )

    axis.text(
        1,
        -0.16,
        "* Partial year through the final available trading day",
        transform=axis.transAxes,
        ha="right",
        fontsize=9,
    )
    figure.tight_layout()
    figure.savefig(output_path, dpi=170, bbox_inches="tight")
    plt.close(figure)


def create_performance_report(
    prices: pd.DataFrame,
    result: BacktestResult,
    config: BacktestConfig,
    output_dir: Path,
) -> tuple[dict[str, object], pd.DataFrame]:
    """Create benchmark data, annual analytics, charts, and comparison files."""

    output_dir.mkdir(parents=True, exist_ok=True)
    buy_hold = build_buy_hold_equity(prices, config)

    equity_with_benchmark = result.equity_curve.copy()
    equity_with_benchmark["BuyHoldEquity"] = buy_hold.equity
    yearly = build_yearly_performance(result, buy_hold.equity, config.initial_cash)

    comparison: dict[str, object] = {
        "strategy": calculate_performance_stats(
            result.equity_curve["Equity"],
            initial_equity=config.initial_cash,
        ),
        "buy_and_hold": calculate_performance_stats(
            buy_hold.equity,
            initial_equity=config.initial_cash,
        ),
        "benchmark_setup": {
            "shares": buy_hold.shares,
            "fill_price": buy_hold.fill_price,
            "remaining_cash": buy_hold.remaining_cash,
            "end_position_marked_to_market": True,
        },
    }

    equity_with_benchmark.to_csv(output_dir / "equity_with_benchmark.csv")
    yearly.to_csv(output_dir / "yearly_performance.csv")
    (output_dir / "comparison_summary.json").write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _save_performance_overview(
        equity_with_benchmark,
        output_dir / "performance_overview.png",
    )
    _save_yearly_returns(yearly, output_dir / "yearly_returns.png")
    return comparison, yearly
