"""Point-in-time covariance estimates and risk-managed portfolio weights."""

import math
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class PortfolioRiskConfig:
    """Transparent controls for a long-only daily equity portfolio."""

    decay_factor: float = 0.94
    min_observations: int = 60
    annualization: int = 252
    correlation_shrinkage: float = 0.25
    target_annual_volatility: float = 0.10
    max_asset_weight: float = 0.40
    max_gross_exposure: float = 1.00

    def __post_init__(self) -> None:
        if not 0 < self.decay_factor < 1:
            raise ValueError("decay_factor must be between 0 and 1")
        if (
            isinstance(self.min_observations, bool)
            or not isinstance(self.min_observations, int)
            or self.min_observations < 2
        ):
            raise ValueError("min_observations must be an integer of at least 2")
        if (
            isinstance(self.annualization, bool)
            or not isinstance(self.annualization, int)
            or self.annualization <= 0
        ):
            raise ValueError("annualization must be a positive integer")

        fractions = {
            "correlation_shrinkage": self.correlation_shrinkage,
            "target_annual_volatility": self.target_annual_volatility,
            "max_asset_weight": self.max_asset_weight,
            "max_gross_exposure": self.max_gross_exposure,
        }
        for name, value in fractions.items():
            if not math.isfinite(value) or not 0 < value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.max_asset_weight > self.max_gross_exposure:
            raise ValueError("max_asset_weight cannot exceed max_gross_exposure")


def build_close_frame(price_frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Align adjusted closes for every asset on common trading dates."""

    if not price_frames:
        raise ValueError("At least one price frame is required")

    closes: dict[str, pd.Series] = {}
    for ticker, frame in price_frames.items():
        if "Close" not in frame:
            raise ValueError(f"Missing Close column for {ticker}")
        if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
            raise ValueError(f"Invalid price index for {ticker}")
        close = frame["Close"].astype(float)
        if close.isna().any() or (close <= 0).any():
            raise ValueError(f"Close prices for {ticker} must be positive and complete")
        closes[ticker] = close

    result = pd.DataFrame(closes).dropna(how="any")
    if result.empty:
        raise ValueError("Price frames have no common dates")
    result.index = pd.DatetimeIndex(result.index, name="Date")
    return result


def calculate_log_returns(close_prices: pd.DataFrame) -> pd.DataFrame:
    """Calculate aligned close-to-close log returns."""

    if close_prices.empty or (close_prices <= 0).any().any():
        raise ValueError("Close prices must be positive and non-empty")
    returns = np.log(close_prices / close_prices.shift(1)).dropna(how="any")
    returns.index = pd.DatetimeIndex(returns.index, name="Date")
    return returns


def shrink_covariance(
    covariance: pd.DataFrame,
    shrinkage: float,
) -> pd.DataFrame:
    """Shrink correlations toward zero while preserving asset variances."""

    if not 0 <= shrinkage <= 1:
        raise ValueError("shrinkage must be between 0 and 1")
    if covariance.empty or covariance.shape[0] != covariance.shape[1]:
        raise ValueError("covariance must be a non-empty square matrix")
    if covariance.index.tolist() != covariance.columns.tolist():
        raise ValueError("covariance labels must match")

    values = covariance.to_numpy(dtype=float)
    variances = np.diag(values)
    if not np.isfinite(values).all() or (variances <= 0).any():
        raise ValueError("covariance must be finite with positive variances")

    volatility = np.sqrt(variances)
    scale = np.outer(volatility, volatility)
    correlation = values / scale
    shrunk_correlation = (1.0 - shrinkage) * correlation + shrinkage * np.eye(
        len(covariance)
    )
    shrunk = shrunk_correlation * scale
    return pd.DataFrame(shrunk, index=covariance.index, columns=covariance.columns)


def calculate_ewma_covariances(
    returns: pd.DataFrame,
    config: PortfolioRiskConfig | None = None,
) -> dict[pd.Timestamp, pd.DataFrame]:
    """Return annualized covariance forecasts available after each close.

    The recursion is ``S_t = lambda*S_(t-1) + (1-lambda)*r_t*r_t'``.
    A matrix dated ``t`` may therefore be used for an order no earlier than the
    next session. No future return enters an earlier covariance estimate.
    """

    config = config or PortfolioRiskConfig()
    if returns.empty or returns.isna().any().any():
        raise ValueError("Returns must be non-empty and complete")
    if returns.index.has_duplicates or not returns.index.is_monotonic_increasing:
        raise ValueError("Returns index must be unique and sorted")

    assets = returns.columns.tolist()
    covariance_state: np.ndarray | None = None
    history: dict[pd.Timestamp, pd.DataFrame] = {}

    for observation_number, (date, row) in enumerate(returns.iterrows(), start=1):
        vector = row.to_numpy(dtype=float)
        if not np.isfinite(vector).all():
            raise ValueError("Returns must be finite")
        innovation = np.outer(vector, vector)
        if covariance_state is None:
            covariance_state = innovation
        else:
            covariance_state = (
                config.decay_factor * covariance_state
                + (1.0 - config.decay_factor) * innovation
            )

        if observation_number < config.min_observations:
            continue
        annualized = pd.DataFrame(
            covariance_state * config.annualization,
            index=assets,
            columns=assets,
        )
        history[pd.Timestamp(date)] = shrink_covariance(
            annualized,
            config.correlation_shrinkage,
        )

    return history


def _allocate_capped_long_weights(
    raw_signal: pd.Series,
    gross_limit: float,
    asset_cap: float,
) -> pd.Series:
    weights = pd.Series(0.0, index=raw_signal.index)
    active = raw_signal[raw_signal > 0].copy()
    remaining = gross_limit

    while not active.empty and remaining > 1e-12:
        proposal = active / active.sum() * remaining
        capacity = asset_cap - weights.loc[active.index]
        capped = proposal >= capacity - 1e-12
        if not capped.any():
            weights.loc[active.index] += proposal
            break
        capped_assets = active.index[capped]
        weights.loc[capped_assets] += capacity.loc[capped_assets]
        remaining = gross_limit - float(weights.sum())
        active = active.drop(capped_assets)

    return weights.clip(lower=0.0, upper=asset_cap)


def calculate_risk_managed_weights(
    scores: pd.DataFrame,
    covariance_history: dict[pd.Timestamp, pd.DataFrame],
    config: PortfolioRiskConfig | None = None,
) -> pd.DataFrame:
    """Convert non-negative alpha scores into volatility-targeted weights."""

    config = config or PortfolioRiskConfig()
    if scores.empty:
        raise ValueError("Scores must be non-empty")
    if scores.index.has_duplicates or not scores.index.is_monotonic_increasing:
        raise ValueError("Scores index must be unique and sorted")

    rows: list[dict[str, float | pd.Timestamp]] = []
    z_95 = 1.6448536269514722
    normal_density_at_z = math.exp(-(z_95**2) / 2.0) / math.sqrt(2.0 * math.pi)
    es_multiplier_95 = normal_density_at_z / 0.05

    for date, covariance in covariance_history.items():
        assets = covariance.columns
        if not set(assets).issubset(scores.columns) or date not in scores.index:
            continue
        score = scores.loc[date, assets].astype(float).clip(lower=0.0).fillna(0.0)
        asset_volatility = pd.Series(
            np.sqrt(np.diag(covariance.to_numpy(dtype=float))),
            index=assets,
        )
        raw_signal = score / asset_volatility
        conviction_gross_limit = (
            config.max_gross_exposure * min(1.0, float(score.max()))
        )
        weights = _allocate_capped_long_weights(
            raw_signal,
            conviction_gross_limit,
            config.max_asset_weight,
        )

        covariance_values = covariance.to_numpy(dtype=float)
        weight_values = weights.to_numpy(dtype=float)
        portfolio_variance = float(
            weight_values @ covariance_values @ weight_values
        )
        unscaled_volatility = math.sqrt(max(0.0, portfolio_variance))
        volatility_scale = (
            min(1.0, config.target_annual_volatility / unscaled_volatility)
            if unscaled_volatility > 0
            else 1.0
        )
        weights *= volatility_scale

        weight_values = weights.to_numpy(dtype=float)
        portfolio_variance = float(
            weight_values @ covariance_values @ weight_values
        )
        annual_volatility = math.sqrt(max(0.0, portfolio_variance))
        daily_volatility = annual_volatility / math.sqrt(config.annualization)
        marginal_variance = covariance_values @ weight_values
        if portfolio_variance > 0:
            risk_contribution = weight_values * marginal_variance / portfolio_variance
        else:
            risk_contribution = np.zeros(len(assets))

        output: dict[str, float | pd.Timestamp] = {
            "Date": date,
            "GrossExposure": float(weights.sum()),
            "CashWeight": float(1.0 - weights.sum()),
            "ForecastAnnualVolatility": annual_volatility,
            "OneDayVaR95": z_95 * daily_volatility,
            "OneDayExpectedShortfall95": es_multiplier_95 * daily_volatility,
        }
        for position, asset in enumerate(assets):
            output[f"{asset}Score"] = float(score.loc[asset])
            output[f"{asset}AnnualVolatility"] = float(
                asset_volatility.loc[asset]
            )
            output[f"{asset}Weight"] = float(weights.loc[asset])
            output[f"{asset}RiskContribution"] = float(
                risk_contribution[position]
            )
        rows.append(output)

    if not rows:
        raise ValueError("Scores and covariance history have no common dates")
    result = pd.DataFrame(rows).set_index("Date")
    result.index = pd.DatetimeIndex(result.index, name="Date")
    return result


def covariance_to_correlation(covariance: pd.DataFrame) -> pd.DataFrame:
    """Convert a covariance matrix into a correlation matrix."""

    volatility = np.sqrt(np.diag(covariance.to_numpy(dtype=float)))
    values = covariance.to_numpy(dtype=float) / np.outer(volatility, volatility)
    return pd.DataFrame(values, index=covariance.index, columns=covariance.columns)


def historical_fixed_weight_stress(
    returns: pd.DataFrame,
    weights: pd.Series,
    lookback: int = 252,
) -> dict[str, float | str | int | None]:
    """Replay current weights through recent realized one-day return scenarios."""

    if isinstance(lookback, bool) or not isinstance(lookback, int) or lookback <= 0:
        raise ValueError("lookback must be a positive integer")
    aligned = returns.loc[:, weights.index].tail(lookback)
    portfolio_returns = aligned @ weights
    if portfolio_returns.empty:
        return {
            "observations": 0,
            "worst_date": None,
            "worst_return": None,
            "historical_var_95": None,
            "historical_expected_shortfall_95": None,
        }
    threshold = float(portfolio_returns.quantile(0.05))
    tail = portfolio_returns[portfolio_returns <= threshold]
    return {
        "observations": len(portfolio_returns),
        "worst_date": portfolio_returns.idxmin().date().isoformat(),
        "worst_return": float(portfolio_returns.min()),
        "historical_var_95": -threshold,
        "historical_expected_shortfall_95": -float(tail.mean()),
    }


def config_as_dict(config: PortfolioRiskConfig) -> dict[str, float | int]:
    """Return a JSON-ready configuration mapping."""

    return asdict(config)
