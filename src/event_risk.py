"""Point-in-time event-risk overlays for the price-based alpha model."""

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import time
from enum import IntEnum

import pandas as pd


class NewsSeverity(IntEnum):
    """Conservative severity assigned by transparent headline rules."""

    NONE = 0
    REVIEW = 1
    MATERIAL = 2


MATERIAL_NEWS_PHRASES = (
    "cuts guidance",
    "cut guidance",
    "lowers guidance",
    "lowered guidance",
    "withdraws guidance",
    "withdrawn guidance",
    "profit warning",
    "going concern",
    "accounting irregularit",
    "restates financial",
    "restate financial",
    "material weakness",
    "files for bankruptcy",
    "filed for bankruptcy",
    "chapter 11",
    "default on debt",
    "fraud charges",
    "charged with fraud",
    "cease operations",
    "suspends operations",
    "sales ban",
)

REVIEW_NEWS_PHRASES = (
    "misses estimates",
    "missed estimates",
    "misses expectations",
    "missed expectations",
    "downgrade",
    "under investigation",
    "investigation",
    "lawsuit",
    "data breach",
    "ceo resigns",
    "cfo resigns",
    "steps down",
    "layoffs",
    "job cuts",
    "antitrust",
    "recall",
    "warning letter",
    "subpoena",
)

MATERIAL_8K_ITEMS = {"1.03", "4.01", "4.02"}


@dataclass(frozen=True)
class EventRiskConfig:
    """Risk weights applied after the price alpha has been calculated."""

    gap_block_threshold: float = 2.5
    gap_event_weight: float = 0.5
    review_news_weight: float = 0.5
    material_news_weight: float = 0.0
    decision_cutoff: time = time(9, 25)
    market_timezone: str = "America/New_York"

    def __post_init__(self) -> None:
        if not math.isfinite(self.gap_block_threshold):
            raise ValueError("gap_block_threshold must be finite")
        if self.gap_block_threshold <= 0:
            raise ValueError("gap_block_threshold must be positive")

        weights = {
            "gap_event_weight": self.gap_event_weight,
            "review_news_weight": self.review_news_weight,
            "material_news_weight": self.material_news_weight,
        }
        for name, value in weights.items():
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")


def classify_news_event(headline: str, summary: str = "") -> NewsSeverity:
    """Flag potentially adverse events using auditable phrase lists.

    This deliberately does not claim to understand economic meaning. A
    ``MATERIAL`` result means the article requires review before a trade, not
    that the headline has proven permanent damage to company value.
    """

    text = " ".join(f"{headline} {summary}".casefold().split())
    if any(phrase in text for phrase in MATERIAL_NEWS_PHRASES):
        return NewsSeverity.MATERIAL
    if any(phrase in text for phrase in REVIEW_NEWS_PHRASES):
        return NewsSeverity.REVIEW
    return NewsSeverity.NONE


def _normalise_session_index(price_index: pd.Index) -> pd.DatetimeIndex:
    sessions = pd.DatetimeIndex(pd.to_datetime(price_index))
    if sessions.tz is not None:
        sessions = sessions.tz_convert("America/New_York").tz_localize(None)
    sessions = sessions.normalize()
    if sessions.has_duplicates:
        raise ValueError("Price index contains duplicate sessions")
    return sessions.sort_values()


def assign_news_to_signal_dates(
    articles: pd.DataFrame,
    price_index: pd.Index,
    config: EventRiskConfig | None = None,
) -> pd.DataFrame:
    """Assign each article to the signal that could use it before execution.

    A signal is formed after session ``t`` and may trade at the next open.
    News published by 09:25 ET on a trading day belongs to the prior session's
    signal. News after that cutoff belongs to the current session's signal.
    Weekend and holiday news belongs to the latest preceding session.
    """

    config = config or EventRiskConfig()
    if "CreatedAt" not in articles.columns:
        raise ValueError("Missing required column: CreatedAt")

    sessions = _normalise_session_index(price_index)
    result = articles.copy()
    created_at = pd.to_datetime(result["CreatedAt"], utc=True, errors="raise")
    local_created_at = created_at.dt.tz_convert(config.market_timezone)

    signal_dates: list[pd.Timestamp | pd.NaTType] = []
    session_set = set(sessions)

    for published_at in local_created_at:
        local_date = pd.Timestamp(published_at.date())
        insertion = int(sessions.searchsorted(local_date, side="left"))

        if local_date in session_set:
            if published_at.time() <= config.decision_cutoff:
                target_position = insertion - 1
            else:
                target_position = insertion
        else:
            target_position = int(sessions.searchsorted(local_date, side="right")) - 1

        if target_position < 0 or target_position >= len(sessions):
            signal_dates.append(pd.NaT)
        else:
            signal_dates.append(sessions[target_position])

    result["CreatedAt"] = created_at
    result["SignalDate"] = pd.DatetimeIndex(signal_dates)
    return result


def _article_mentions_ticker(value: object, ticker: str) -> bool:
    ticker = ticker.upper()
    if isinstance(value, str):
        cleaned = value.replace("[", "").replace("]", "").replace('"', "")
        symbols = {
            symbol.strip().upper()
            for symbol in cleaned.replace("|", ",").split(",")
            if symbol.strip()
        }
        return ticker in symbols
    if isinstance(value, Iterable) and not isinstance(value, (bytes, dict)):
        return ticker in {str(symbol).upper() for symbol in value}
    return False


def build_daily_news_risk(
    articles: pd.DataFrame,
    price_index: pd.Index,
    ticker: str,
    config: EventRiskConfig | None = None,
) -> pd.DataFrame:
    """Aggregate timestamped articles into daily, point-in-time risk flags."""

    config = config or EventRiskConfig()
    required = {"CreatedAt", "Headline", "Symbols"}
    missing = required.difference(articles.columns)
    if missing:
        missing_names = ", ".join(sorted(missing))
        raise ValueError(f"Missing required columns: {missing_names}")

    relevant = articles.loc[
        articles["Symbols"].map(lambda value: _article_mentions_ticker(value, ticker))
    ].copy()
    if relevant.empty:
        return pd.DataFrame(
            columns=[
                "NewsCount",
                "ReviewNewsCount",
                "MaterialNewsCount",
                "NewsSeverity",
                "NewsEventWeight",
                "RequiresManualReview",
            ],
            index=pd.DatetimeIndex([], name="Date"),
        )

    relevant = assign_news_to_signal_dates(relevant, price_index, config)
    relevant = relevant.dropna(subset=["SignalDate"])
    summaries = relevant.get("Summary", pd.Series("", index=relevant.index)).fillna("")
    relevant["SeverityValue"] = [
        int(classify_news_event(headline, summary))
        for headline, summary in zip(
            relevant["Headline"].fillna(""),
            summaries,
            strict=True,
        )
    ]
    relevant["IsReview"] = relevant["SeverityValue"] == int(NewsSeverity.REVIEW)
    relevant["IsMaterial"] = relevant["SeverityValue"] == int(
        NewsSeverity.MATERIAL
    )

    daily = relevant.groupby("SignalDate").agg(
        NewsCount=("SeverityValue", "size"),
        ReviewNewsCount=("IsReview", "sum"),
        MaterialNewsCount=("IsMaterial", "sum"),
        SeverityValue=("SeverityValue", "max"),
    )
    daily.index = pd.DatetimeIndex(daily.index, name="Date")
    daily["NewsSeverity"] = daily["SeverityValue"].map(
        {
            int(NewsSeverity.NONE): "NONE",
            int(NewsSeverity.REVIEW): "REVIEW",
            int(NewsSeverity.MATERIAL): "MATERIAL",
        }
    )
    daily["NewsEventWeight"] = daily["SeverityValue"].map(
        {
            int(NewsSeverity.NONE): 1.0,
            int(NewsSeverity.REVIEW): config.review_news_weight,
            int(NewsSeverity.MATERIAL): config.material_news_weight,
        }
    )
    daily["RequiresManualReview"] = daily["MaterialNewsCount"] > 0
    return daily.drop(columns="SeverityValue")


def classify_filing_event(form: str, items: str = "") -> NewsSeverity:
    """Classify tracked SEC forms without reading future filing contents."""

    item_set = {item.strip() for item in items.split(",") if item.strip()}
    if form.startswith("8-K") and item_set.intersection(MATERIAL_8K_ITEMS):
        return NewsSeverity.MATERIAL
    if form.startswith(("8-K", "10-Q", "10-K")):
        return NewsSeverity.REVIEW
    return NewsSeverity.NONE


def build_daily_filing_risk(
    filings: pd.DataFrame,
    price_index: pd.Index,
    ticker: str,
    config: EventRiskConfig | None = None,
) -> pd.DataFrame:
    """Aggregate SEC filings by the signal date that could observe them."""

    config = config or EventRiskConfig()
    required = {"Ticker", "AcceptedAt", "Form", "Items"}
    missing = required.difference(filings.columns)
    if missing:
        missing_names = ", ".join(sorted(missing))
        raise ValueError(f"Missing required columns: {missing_names}")

    relevant = filings.loc[filings["Ticker"].str.upper() == ticker.upper()].copy()
    if relevant.empty:
        return pd.DataFrame(
            columns=[
                "FilingCount",
                "MaterialFilingCount",
                "FilingSeverity",
                "FilingEventWeight",
                "RequiresFilingReview",
            ],
            index=pd.DatetimeIndex([], name="Date"),
        )

    relevant["CreatedAt"] = relevant["AcceptedAt"]
    relevant = assign_news_to_signal_dates(relevant, price_index, config)
    relevant = relevant.dropna(subset=["SignalDate"])
    relevant["SeverityValue"] = [
        int(classify_filing_event(form, items))
        for form, items in zip(relevant["Form"], relevant["Items"], strict=True)
    ]
    relevant["IsMaterial"] = relevant["SeverityValue"] == int(
        NewsSeverity.MATERIAL
    )
    daily = relevant.groupby("SignalDate").agg(
        FilingCount=("SeverityValue", "size"),
        MaterialFilingCount=("IsMaterial", "sum"),
        SeverityValue=("SeverityValue", "max"),
    )
    daily.index = pd.DatetimeIndex(daily.index, name="Date")
    daily["FilingSeverity"] = daily["SeverityValue"].map(
        {
            int(NewsSeverity.NONE): "NONE",
            int(NewsSeverity.REVIEW): "REVIEW",
            int(NewsSeverity.MATERIAL): "MATERIAL",
        }
    )
    daily["FilingEventWeight"] = daily["SeverityValue"].map(
        {
            int(NewsSeverity.NONE): 1.0,
            int(NewsSeverity.REVIEW): config.review_news_weight,
            int(NewsSeverity.MATERIAL): config.material_news_weight,
        }
    )
    daily["RequiresFilingReview"] = daily["MaterialFilingCount"] > 0
    return daily.drop(columns="SeverityValue")


def apply_event_risk_overlay(
    alpha_frame: pd.DataFrame,
    daily_news_risk: pd.DataFrame | None = None,
    config: EventRiskConfig | None = None,
    daily_filing_risk: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Combine gap and news risk with an existing price-based alpha score."""

    config = config or EventRiskConfig()
    required = {
        "Open",
        "Close",
        "LaggedDailyVolatility",
        "AlphaScore",
        "AlphaActive",
    }
    missing = required.difference(alpha_frame.columns)
    if missing:
        missing_names = ", ".join(sorted(missing))
        raise ValueError(f"Missing required columns: {missing_names}")

    result = alpha_frame.copy()
    previous_close = result["Close"].shift(1)
    result["GapReturn"] = result["Open"] / previous_close - 1.0
    result["GapShockZ"] = result["GapReturn"] / result[
        "LaggedDailyVolatility"
    ].replace(0.0, pd.NA)
    result["GapEventRisk"] = result["GapShockZ"] <= -config.gap_block_threshold
    result["GapEventWeight"] = 1.0
    result.loc[result["GapEventRisk"], "GapEventWeight"] = config.gap_event_weight

    if daily_news_risk is not None:
        if daily_news_risk.index.has_duplicates:
            raise ValueError("Daily news risk index contains duplicate dates")
        result = result.join(daily_news_risk, how="left")

    if daily_filing_risk is not None:
        if daily_filing_risk.index.has_duplicates:
            raise ValueError("Daily filing risk index contains duplicate dates")
        result = result.join(daily_filing_risk, how="left")

    integer_defaults = {
        "NewsCount": 0,
        "ReviewNewsCount": 0,
        "MaterialNewsCount": 0,
        "FilingCount": 0,
        "MaterialFilingCount": 0,
    }
    for column, default in integer_defaults.items():
        if column not in result:
            result[column] = default
        result[column] = result[column].fillna(default).astype(int)

    if "NewsSeverity" not in result:
        result["NewsSeverity"] = "NONE"
    result["NewsSeverity"] = result["NewsSeverity"].fillna("NONE")

    if "NewsEventWeight" not in result:
        result["NewsEventWeight"] = 1.0
    result["NewsEventWeight"] = result["NewsEventWeight"].fillna(1.0)

    if "RequiresManualReview" not in result:
        result["RequiresManualReview"] = False
    result["RequiresManualReview"] = (
        result["RequiresManualReview"].fillna(False).astype(bool)
    )

    if "FilingSeverity" not in result:
        result["FilingSeverity"] = "NONE"
    result["FilingSeverity"] = result["FilingSeverity"].fillna("NONE")
    if "FilingEventWeight" not in result:
        result["FilingEventWeight"] = 1.0
    result["FilingEventWeight"] = result["FilingEventWeight"].fillna(1.0)
    if "RequiresFilingReview" not in result:
        result["RequiresFilingReview"] = False
    result["RequiresFilingReview"] = (
        result["RequiresFilingReview"].fillna(False).astype(bool)
    )
    result["RequiresManualReview"] = (
        result["RequiresManualReview"] | result["RequiresFilingReview"]
    )

    result["EventWeight"] = result[
        ["GapEventWeight", "NewsEventWeight", "FilingEventWeight"]
    ].min(axis=1)
    result["EventAdjustedAlpha"] = result["AlphaScore"] * result["EventWeight"]
    result["TradeEligible"] = (
        result["AlphaActive"]
        & ~result["RequiresManualReview"]
        & result["EventAdjustedAlpha"].gt(0.0)
    )
    return result
