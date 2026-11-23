from datetime import time

import pandas as pd
import pandas.testing as pdt
import pytest

from src.event_risk import (
    EventRiskConfig,
    NewsSeverity,
    apply_event_risk_overlay,
    assign_news_to_signal_dates,
    build_daily_filing_risk,
    build_daily_news_risk,
    classify_filing_event,
    classify_news_event,
)


def test_news_classifier_separates_review_and_material_events() -> None:
    assert (
        classify_news_event("Company lowers guidance after weak demand")
        == NewsSeverity.MATERIAL
    )
    assert classify_news_event("Broker downgrades shares") == NewsSeverity.REVIEW
    assert classify_news_event("Company opens new research center") == NewsSeverity.NONE

    assert classify_filing_event("8-K", "4.02,9.01") == NewsSeverity.MATERIAL
    assert classify_filing_event("10-Q") == NewsSeverity.REVIEW


def test_news_is_mapped_to_information_available_before_execution() -> None:
    sessions = pd.to_datetime(["2024-01-05", "2024-01-08", "2024-01-09"])
    articles = pd.DataFrame(
        {
            "CreatedAt": [
                "2024-01-05T15:00:00Z",  # Friday 10:00 ET -> Friday signal
                "2024-01-05T22:00:00Z",  # Friday 17:00 ET -> Friday signal
                "2024-01-07T17:00:00Z",  # Sunday -> Friday signal
                "2024-01-08T13:00:00Z",  # Monday 08:00 ET -> Friday signal
                "2024-01-08T15:00:00Z",  # Monday 10:00 ET -> Monday signal
            ]
        }
    )

    result = assign_news_to_signal_dates(articles, sessions)

    assert result["SignalDate"].tolist() == [
        pd.Timestamp("2024-01-05"),
        pd.Timestamp("2024-01-05"),
        pd.Timestamp("2024-01-05"),
        pd.Timestamp("2024-01-05"),
        pd.Timestamp("2024-01-08"),
    ]


def test_daily_news_risk_uses_most_severe_article() -> None:
    sessions = pd.to_datetime(["2024-01-05", "2024-01-08"])
    articles = pd.DataFrame(
        {
            "CreatedAt": [
                "2024-01-05T16:00:00Z",
                "2024-01-05T17:00:00Z",
            ],
            "Headline": [
                "Broker downgrades NVDA",
                "NVDA lowers guidance",
            ],
            "Summary": ["", "Weak demand"],
            "Symbols": [["NVDA"], ["NVDA", "MSFT"]],
        }
    )

    result = build_daily_news_risk(articles, sessions, "NVDA")

    day = result.loc[pd.Timestamp("2024-01-05")]
    assert day["NewsCount"] == 2
    assert day["ReviewNewsCount"] == 1
    assert day["MaterialNewsCount"] == 1
    assert day["NewsSeverity"] == "MATERIAL"
    assert day["NewsEventWeight"] == 0
    assert bool(day["RequiresManualReview"])


def test_daily_news_risk_accepts_parquet_array_like_symbols() -> None:
    class SymbolArray:
        def __iter__(self):
            return iter(["NVDA"])

    sessions = pd.to_datetime(["2024-01-05", "2024-01-08"])
    articles = pd.DataFrame(
        {
            "CreatedAt": ["2024-01-05T16:00:00Z"],
            "Headline": ["NVDA opens a research center"],
            "Summary": [""],
            "Symbols": [SymbolArray()],
        }
    )

    result = build_daily_news_risk(articles, sessions, "NVDA")

    assert result.loc[pd.Timestamp("2024-01-05"), "NewsCount"] == 1


def make_alpha_frame() -> pd.DataFrame:
    index = pd.to_datetime(["2024-01-04", "2024-01-05", "2024-01-08"])
    return pd.DataFrame(
        {
            "Open": [100.0, 90.0, 96.0],
            "Close": [100.0, 95.0, 97.0],
            "LaggedDailyVolatility": [0.02, 0.02, 0.02],
            "AlphaScore": [0.2, 0.8, 0.4],
            "AlphaActive": [True, True, True],
        },
        index=index,
    )


def test_gap_and_news_risk_reduce_or_block_alpha() -> None:
    frame = make_alpha_frame()
    news_risk = pd.DataFrame(
        {
            "NewsCount": [1],
            "ReviewNewsCount": [0],
            "MaterialNewsCount": [1],
            "NewsSeverity": ["MATERIAL"],
            "NewsEventWeight": [0.0],
            "RequiresManualReview": [True],
        },
        index=pd.DatetimeIndex(["2024-01-08"], name="Date"),
    )

    result = apply_event_risk_overlay(frame, news_risk)

    gap_day = result.loc[pd.Timestamp("2024-01-05")]
    assert gap_day["GapShockZ"] == pytest.approx(-5.0)
    assert bool(gap_day["GapEventRisk"])
    assert gap_day["EventAdjustedAlpha"] == pytest.approx(0.4)
    assert bool(gap_day["TradeEligible"])

    news_day = result.loc[pd.Timestamp("2024-01-08")]
    assert news_day["EventAdjustedAlpha"] == 0
    assert bool(news_day["RequiresManualReview"])
    assert not bool(news_day["TradeEligible"])


def test_sec_filing_risk_uses_acceptance_time_and_blocks_material_item() -> None:
    sessions = pd.to_datetime(["2024-01-04", "2024-01-05", "2024-01-08"])
    filings = pd.DataFrame(
        {
            "Ticker": ["NVDA"],
            "AcceptedAt": ["2024-01-05T22:00:00Z"],
            "Form": ["8-K"],
            "Items": ["4.02,9.01"],
        }
    )

    daily = build_daily_filing_risk(filings, sessions, "NVDA")

    row = daily.loc[pd.Timestamp("2024-01-05")]
    assert row["FilingSeverity"] == "MATERIAL"
    assert row["FilingEventWeight"] == 0
    assert bool(row["RequiresFilingReview"])


def test_overlay_does_not_modify_input_and_validates_weights() -> None:
    frame = make_alpha_frame()
    original = frame.copy()

    apply_event_risk_overlay(frame)

    pdt.assert_frame_equal(frame, original)
    with pytest.raises(ValueError, match="between 0 and 1"):
        EventRiskConfig(review_news_weight=1.5)
    with pytest.raises(ValueError, match="positive"):
        EventRiskConfig(gap_block_threshold=0)


def test_custom_decision_cutoff_is_respected() -> None:
    sessions = pd.to_datetime(["2024-01-05", "2024-01-08"])
    articles = pd.DataFrame({"CreatedAt": ["2024-01-08T14:20:00Z"]})
    config = EventRiskConfig(decision_cutoff=time(9, 15))

    result = assign_news_to_signal_dates(articles, sessions, config)

    assert result["SignalDate"].iloc[0] == pd.Timestamp("2024-01-08")
