"""Deterministic daily-bar backtest engine for the Donchian strategy."""

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

from src.risk import (
    PositionLot,
    calculate_atr,
    calculate_atr_position_size,
    calculate_initial_stop,
    calculate_next_add_price,
    calculate_pyramid_add_size,
    calculate_raised_stop,
)
from src.strategy import add_donchian_signals


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: float = 100_000.0
    system1_entry_window: int = 20
    system1_exit_window: int = 10
    system2_entry_window: int = 55
    system2_exit_window: int = 20
    atr_period: int = 20
    risk_fraction: float = 0.01
    stop_atr_multiple: float = 2.0
    add_interval_atr: float = 0.5
    max_units: int = 4
    max_total_notional: float = 98_000.0
    cash_fraction_limit: float = 0.98
    slippage_fraction: float = 0.0005

    def __post_init__(self) -> None:
        positive_values = {
            "initial_cash": self.initial_cash,
            "max_total_notional": self.max_total_notional,
        }
        for name, value in positive_values.items():
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive finite number")

        if self.max_total_notional > self.initial_cash:
            raise ValueError("max_total_notional cannot exceed initial_cash")

        if not 0 <= self.slippage_fraction < 1:
            raise ValueError("slippage_fraction must be in [0, 1)")

        integer_values = {
            "system1_entry_window": self.system1_entry_window,
            "system1_exit_window": self.system1_exit_window,
            "system2_entry_window": self.system2_entry_window,
            "system2_exit_window": self.system2_exit_window,
            "atr_period": self.atr_period,
            "max_units": self.max_units,
        }
        for name, value in integer_values.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class BacktestResult:
    equity_curve: pd.DataFrame
    fills: pd.DataFrame
    summary: dict[str, object]


@dataclass(frozen=True)
class _VirtualSystem1Breakout:
    entry_price: float
    atr: float

    @property
    def stop_price(self) -> float:
        return calculate_initial_stop(self.entry_price, self.atr)


@dataclass
class _BacktestState:
    cash: float
    lots: list[PositionLot] = field(default_factory=list)
    position_atr: float | None = None
    shared_stop: float | None = None
    next_add_price: float | None = None
    planned_unit_shares: int = 0
    position_system: str | None = None
    virtual_system1: _VirtualSystem1Breakout | None = None
    last_system1_was_winner: bool = False
    system1_skipped_breakouts: int = 0
    virtual_system1_winners: int = 0
    virtual_system1_losers: int = 0
    campaign_id: int = 0

    @property
    def total_shares(self) -> int:
        return sum(lot.shares for lot in self.lots)

    @property
    def position_cost(self) -> float:
        return sum(lot.entry_price * lot.shares for lot in self.lots)

    def clear_position(self) -> None:
        self.lots.clear()
        self.position_atr = None
        self.shared_stop = None
        self.next_add_price = None
        self.planned_unit_shares = 0
        self.position_system = None


FILL_COLUMNS = [
    "Date",
    "SignalDate",
    "Campaign",
    "System",
    "Event",
    "Side",
    "ReferencePrice",
    "FillPrice",
    "N",
    "Shares",
    "UnitsAfter",
    "CashAfter",
    "StopAfter",
    "RealizedPnL",
]


def _append_fill(
    fills: list[dict[str, object]],
    *,
    date: pd.Timestamp,
    signal_date: pd.Timestamp,
    state: _BacktestState,
    system: str,
    event: str,
    side: str,
    reference_price: float,
    fill_price: float,
    shares: int,
    n_value: float | None = None,
    realized_pnl: float | None = None,
) -> None:
    fills.append(
        {
            "Date": date,
            "SignalDate": signal_date,
            "Campaign": state.campaign_id,
            "System": system,
            "Event": event,
            "Side": side,
            "ReferencePrice": reference_price,
            "FillPrice": fill_price,
            "N": state.position_atr if n_value is None else n_value,
            "Shares": shares,
            "UnitsAfter": len(state.lots),
            "CashAfter": state.cash,
            "StopAfter": state.shared_stop,
            "RealizedPnL": realized_pnl,
        }
    )


def _exit_all(
    fills: list[dict[str, object]],
    *,
    date: pd.Timestamp,
    signal_date: pd.Timestamp,
    state: _BacktestState,
    reference_price: float,
    event: str,
    slippage_fraction: float,
) -> None:
    shares = state.total_shares
    if shares <= 0:
        return

    fill_price = reference_price * (1 - slippage_fraction)
    proceeds = fill_price * shares
    realized_pnl = proceeds - state.position_cost
    state.cash += proceeds
    system = state.position_system
    n_value = state.position_atr
    if system is None:
        raise RuntimeError("Open position has no system label")
    if n_value is None:
        raise RuntimeError("Open position has no N value")
    state.clear_position()

    _append_fill(
        fills,
        date=date,
        signal_date=signal_date,
        state=state,
        system=system,
        event=event,
        side="SELL",
        reference_price=reference_price,
        fill_price=fill_price,
        shares=shares,
        n_value=n_value,
        realized_pnl=realized_pnl,
    )


def _prepare_prices(prices: pd.DataFrame, config: BacktestConfig) -> pd.DataFrame:
    required_columns = {"Open", "High", "Low", "Close", "Volume"}
    missing_columns = required_columns.difference(prices.columns)
    if missing_columns:
        missing = ", ".join(sorted(missing_columns))
        raise ValueError(f"Missing required columns: {missing}")

    if prices.empty:
        raise ValueError("Price data is empty")

    if prices.index.has_duplicates:
        raise ValueError("Price index contains duplicate dates")

    if not prices.index.is_monotonic_increasing:
        raise ValueError("Price index must be sorted in ascending order")

    system1 = add_donchian_signals(
        prices,
        entry_window=config.system1_entry_window,
        exit_window=config.system1_exit_window,
    )
    system2 = add_donchian_signals(
        prices,
        entry_window=config.system2_entry_window,
        exit_window=config.system2_exit_window,
    )
    prepared = prices.copy()
    for prefix, signals in (("System1", system1), ("System2", system2)):
        prepared[f"{prefix}EntryLevel"] = signals["EntryLevel"]
        prepared[f"{prefix}ExitLevel"] = signals["ExitLevel"]
        prepared[f"{prefix}EntrySignal"] = signals["EntrySignal"]
        prepared[f"{prefix}ExitSignal"] = signals["ExitSignal"]
    # An intraday order can only use ATR known before the current bar opens.
    prepared["ATR"] = calculate_atr(prices, period=config.atr_period).shift(1)
    return prepared


def _resolve_virtual_system1(
    state: _BacktestState,
    *,
    open_price: float,
    low_price: float,
    exit_level: float | None,
    allow_gap: bool,
) -> bool:
    """Resolve the theoretical System 1 breakout used by the skip rule."""

    tracker = state.virtual_system1
    if tracker is None:
        return False

    protective_level = tracker.stop_price
    if exit_level is not None:
        protective_level = max(protective_level, exit_level)

    if allow_gap and open_price <= protective_level:
        exit_price = open_price
    elif low_price <= protective_level:
        exit_price = protective_level
    else:
        return False

    state.last_system1_was_winner = exit_price > tracker.entry_price
    if state.last_system1_was_winner:
        state.virtual_system1_winners += 1
    else:
        state.virtual_system1_losers += 1
    state.virtual_system1 = None
    return True


def _try_protective_exit(
    fills: list[dict[str, object]],
    *,
    date: pd.Timestamp,
    state: _BacktestState,
    open_price: float,
    low_price: float,
    exit_level: float | None,
    slippage_fraction: float,
    allow_gap: bool,
) -> bool:
    """Exit at the highest active protective level touched by a daily bar."""

    if not state.lots or state.shared_stop is None:
        return False

    protective_levels = [(state.shared_stop, "ATR_STOP", "GAP_STOP")]
    if exit_level is not None:
        protective_levels.append(
            (exit_level, "DONCHIAN_EXIT", "GAP_DONCHIAN_EXIT")
        )

    protective_level, event, gap_event = max(
        protective_levels,
        key=lambda item: item[0],
    )
    if allow_gap and open_price <= protective_level:
        reference_price = open_price
        event = gap_event
    elif low_price <= protective_level:
        reference_price = protective_level
    else:
        return False

    _exit_all(
        fills,
        date=date,
        signal_date=date,
        state=state,
        reference_price=reference_price,
        event=event,
        slippage_fraction=slippage_fraction,
    )
    return True


def _build_summary(
    equity_curve: pd.DataFrame,
    fills: pd.DataFrame,
    state: _BacktestState,
    config: BacktestConfig,
) -> dict[str, object]:
    equity = equity_curve["Equity"]
    final_equity = float(equity.iloc[-1])
    total_return = final_equity / config.initial_cash - 1

    elapsed_years = max(
        (equity.index[-1] - equity.index[0]).days / 365.25,
        1 / 365.25,
    )
    cagr = (final_equity / config.initial_cash) ** (1 / elapsed_years) - 1

    daily_returns = equity.pct_change().dropna()
    annual_volatility = float(daily_returns.std(ddof=1) * math.sqrt(252))
    if daily_returns.std(ddof=1) > 0:
        sharpe = float(
            daily_returns.mean() / daily_returns.std(ddof=1) * math.sqrt(252)
        )
    else:
        sharpe = None

    drawdown = equity / equity.cummax() - 1
    max_drawdown = float(drawdown.min())
    calmar = cagr / abs(max_drawdown) if max_drawdown < 0 else None

    exit_events = fills[fills["Side"] == "SELL"] if not fills.empty else fills
    completed_campaigns = len(exit_events)
    winning_campaigns = (
        int((exit_events["RealizedPnL"] > 0).sum()) if completed_campaigns else 0
    )

    return {
        "config": asdict(config),
        "start_date": equity.index[0].date().isoformat(),
        "end_date": equity.index[-1].date().isoformat(),
        "initial_cash": config.initial_cash,
        "final_equity": final_equity,
        "total_return": total_return,
        "cagr": cagr,
        "annual_volatility": annual_volatility,
        "sharpe_zero_rate": sharpe,
        "max_drawdown": max_drawdown,
        "calmar": calmar,
        "initial_entries": int((fills["Event"] == "ENTRY").sum()),
        "system1_entries": int(
            ((fills["Event"] == "ENTRY") & (fills["System"] == "S1")).sum()
        ),
        "system2_entries": int(
            ((fills["Event"] == "ENTRY") & (fills["System"] == "S2")).sum()
        ),
        "pyramid_adds": int((fills["Event"] == "ADD").sum()),
        "completed_campaigns": completed_campaigns,
        "winning_campaigns": winning_campaigns,
        "win_rate": winning_campaigns / completed_campaigns
        if completed_campaigns
        else None,
        "open_position_shares": state.total_shares,
        "open_position_units": len(state.lots),
        "last_system1_was_winner": state.last_system1_was_winner,
        "open_virtual_system1_breakout": state.virtual_system1 is not None,
        "system1_skipped_breakouts": state.system1_skipped_breakouts,
        "virtual_system1_winners": state.virtual_system1_winners,
        "virtual_system1_losers": state.virtual_system1_losers,
    }


def run_backtest(
    prices: pd.DataFrame,
    config: BacktestConfig | None = None,
) -> BacktestResult:
    """Run a long-only daily Donchian/ATR backtest without look-ahead."""

    config = config or BacktestConfig()
    prepared = _prepare_prices(prices, config)
    state = _BacktestState(cash=config.initial_cash)
    fill_records: list[dict[str, object]] = []
    equity_records: list[dict[str, object]] = []

    initial_unit_notional_cap = config.max_total_notional / config.max_units

    for date, row in prepared.iterrows():
        date = pd.Timestamp(date)
        open_price = float(row["Open"])
        high_price = float(row["High"])
        low_price = float(row["Low"])
        close_price = float(row["Close"])

        exited_today = False
        entered_today = False
        virtual_resolved_today = _resolve_virtual_system1(
            state,
            open_price=open_price,
            low_price=low_price,
            exit_level=float(row["System1ExitLevel"])
            if pd.notna(row["System1ExitLevel"])
            else None,
            allow_gap=True,
        )

        position_exit_column = (
            "System1ExitLevel"
            if state.position_system == "S1"
            else "System2ExitLevel"
        )
        position_exit_level = (
            float(row[position_exit_column])
            if pd.notna(row[position_exit_column])
            else None
        )

        if state.lots:
            exited_today = _try_protective_exit(
                fill_records,
                date=date,
                state=state,
                open_price=open_price,
                low_price=low_price,
                exit_level=position_exit_level,
                slippage_fraction=config.slippage_fraction,
                allow_gap=True,
            )

        entry_system: str | None = None
        entry_reference: float | None = None
        started_virtual_today = False
        signal_atr = float(row["ATR"]) if pd.notna(row["ATR"]) else None
        if (
            not state.lots
            and not exited_today
            and not virtual_resolved_today
            and signal_atr is not None
        ):
            system1_triggered = bool(row["System1EntrySignal"])
            system2_triggered = bool(row["System2EntrySignal"])
            system1_eligible = not state.last_system1_was_winner

            if state.virtual_system1 is None and system1_triggered:
                system1_reference = max(
                    open_price,
                    float(row["System1EntryLevel"]),
                )
                state.virtual_system1 = _VirtualSystem1Breakout(
                    entry_price=system1_reference,
                    atr=signal_atr,
                )
                started_virtual_today = True
                if system1_eligible:
                    entry_system = "S1"
                    entry_reference = system1_reference
                else:
                    state.system1_skipped_breakouts += 1

            if (
                entry_system is None
                and state.last_system1_was_winner
                and system2_triggered
            ):
                entry_system = "S2"
                entry_reference = max(
                    open_price,
                    float(row["System2EntryLevel"]),
                )

        if entry_system is not None and entry_reference is not None:
            buy_fill = entry_reference * (1 + config.slippage_fraction)
            shares = calculate_atr_position_size(
                equity=state.cash,
                cash=state.cash,
                entry_price=buy_fill,
                atr=signal_atr,
                risk_fraction=config.risk_fraction,
                max_notional=initial_unit_notional_cap,
                cash_fraction_limit=config.cash_fraction_limit,
            )

            if shares > 0:
                state.campaign_id += 1
                state.cash -= buy_fill * shares
                state.lots.append(PositionLot(entry_price=buy_fill, shares=shares))
                state.position_atr = signal_atr
                state.shared_stop = calculate_initial_stop(
                    entry_price=buy_fill,
                    atr=signal_atr,
                    stop_atr_multiple=config.stop_atr_multiple,
                )
                state.next_add_price = calculate_next_add_price(
                    last_fill_price=buy_fill,
                    atr=signal_atr,
                    add_interval_atr=config.add_interval_atr,
                )
                state.planned_unit_shares = shares
                state.position_system = entry_system
                entered_today = True

                _append_fill(
                    fill_records,
                    date=date,
                    signal_date=date,
                    state=state,
                    system=entry_system,
                    event="ENTRY",
                    side="BUY",
                    reference_price=entry_reference,
                    fill_price=buy_fill,
                    shares=shares,
                )

        if entered_today:
            # With daily bars the low may precede the breakout. Assume the
            # adverse sequence: entry first, then any touched protective exit.
            entry_exit_column = (
                "System1ExitLevel"
                if state.position_system == "S1"
                else "System2ExitLevel"
            )
            entry_exit_level = (
                float(row[entry_exit_column])
                if pd.notna(row[entry_exit_column])
                else None
            )
            exited_today = _try_protective_exit(
                fill_records,
                date=date,
                state=state,
                open_price=open_price,
                low_price=low_price,
                exit_level=entry_exit_level,
                slippage_fraction=config.slippage_fraction,
                allow_gap=False,
            )

        if started_virtual_today:
            _resolve_virtual_system1(
                state,
                open_price=open_price,
                low_price=low_price,
                exit_level=float(row["System1ExitLevel"])
                if pd.notna(row["System1ExitLevel"])
                else None,
                allow_gap=False,
            )

        while (
            state.lots
            and not exited_today
            and state.position_atr is not None
            and state.shared_stop is not None
            and state.next_add_price is not None
            and high_price >= state.next_add_price
        ):
            add_reference = max(open_price, state.next_add_price)
            add_fill = add_reference * (1 + config.slippage_fraction)
            add_shares = calculate_pyramid_add_size(
                lots=state.lots,
                planned_unit_shares=state.planned_unit_shares,
                cash=state.cash,
                add_price=add_fill,
                max_units=config.max_units,
                max_total_notional=config.max_total_notional,
                cash_fraction_limit=config.cash_fraction_limit,
            )

            if add_shares <= 0:
                break

            system = state.position_system
            if system is None:
                raise RuntimeError("Open position has no system label")
            state.cash -= add_fill * add_shares
            state.lots.append(PositionLot(entry_price=add_fill, shares=add_shares))
            state.shared_stop = calculate_raised_stop(
                current_stop=state.shared_stop,
                latest_fill_price=add_fill,
                atr=state.position_atr,
                stop_atr_multiple=config.stop_atr_multiple,
            )
            state.next_add_price = calculate_next_add_price(
                last_fill_price=add_fill,
                atr=state.position_atr,
                add_interval_atr=config.add_interval_atr,
            )

            _append_fill(
                fill_records,
                date=date,
                signal_date=date,
                state=state,
                system=system,
                event="ADD",
                side="BUY",
                reference_price=add_reference,
                fill_price=add_fill,
                shares=add_shares,
            )

            # The raised stop only exists after the add. When the same daily
            # bar also touches it, assume the adverse add-then-stop sequence.
            if low_price <= state.shared_stop:
                _exit_all(
                    fill_records,
                    date=date,
                    signal_date=date,
                    state=state,
                    reference_price=state.shared_stop,
                    event="ATR_STOP",
                    slippage_fraction=config.slippage_fraction,
                )
                exited_today = True

        if state.cash < -1e-8:
            raise RuntimeError("Backtest produced negative cash")
        state.cash = max(0.0, state.cash)

        position_value = state.total_shares * close_price
        equity = state.cash + position_value
        equity_records.append(
            {
                "Date": date,
                "Cash": state.cash,
                "PositionShares": state.total_shares,
                "PositionValue": position_value,
                "Equity": equity,
                "Units": len(state.lots),
                "System": state.position_system,
                "Stop": state.shared_stop,
                "NextAddPrice": state.next_add_price,
                "LastSystem1Winner": state.last_system1_was_winner,
                "VirtualSystem1Active": state.virtual_system1 is not None,
                "System1Breakout": bool(row["System1EntrySignal"]),
                "System2Breakout": bool(row["System2EntrySignal"]),
            }
        )

    equity_curve = pd.DataFrame(equity_records).set_index("Date")
    equity_curve.index.name = "Date"

    fills = pd.DataFrame(fill_records, columns=FILL_COLUMNS)
    summary = _build_summary(equity_curve, fills, state, config)
    return BacktestResult(equity_curve=equity_curve, fills=fills, summary=summary)


def save_backtest_result(result: BacktestResult, output_dir: Path) -> None:
    """Save deterministic tabular outputs and a JSON summary."""

    output_dir.mkdir(parents=True, exist_ok=True)
    result.equity_curve.to_csv(output_dir / "equity_curve.csv")
    result.fills.to_csv(output_dir / "fills.csv", index=False)
    (output_dir / "summary.json").write_text(
        json.dumps(result.summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
