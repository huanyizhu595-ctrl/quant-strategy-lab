import pandas as pd
import pytest

from src.filings import normalize_sec_submissions
from src.news import normalize_marketaux_response


def test_normalize_marketaux_response_preserves_timestamp_and_symbols() -> None:
    payload = {
        "meta": {"returned": 1},
        "data": [
            {
                "uuid": "article-1",
                "title": "Nvidia opens a new research center",
                "description": "Expansion continues.",
                "published_at": "2024-01-05T22:00:00Z",
                "source": "example.com",
                "url": "https://example.com/article-1",
                "entities": [
                    {"symbol": "NVDA", "sentiment_score": 0.25},
                    {"symbol": "MSFT", "sentiment_score": 0.1},
                ],
            }
        ],
    }

    result = normalize_marketaux_response(payload, "NVDA")

    assert result.loc[0, "ArticleId"] == "article-1"
    assert result.loc[0, "CreatedAt"] == pd.Timestamp(
        "2024-01-05T22:00:00Z"
    )
    assert result.loc[0, "Symbols"] == ["MSFT", "NVDA"]
    assert result.loc[0, "SentimentScore"] == pytest.approx(0.25)


def test_normalize_marketaux_response_rejects_error_payload() -> None:
    with pytest.raises(RuntimeError, match="Marketaux error"):
        normalize_marketaux_response({"error": {"message": "bad token"}}, "NVDA")


def test_normalize_sec_submissions_filters_forms_and_builds_urls() -> None:
    payload = {
        "filings": {
            "recent": {
                "accessionNumber": ["0001045810-24-000001", "0001045810-24-000002"],
                "form": ["8-K", "4"],
                "filingDate": ["2024-01-05", "2024-01-06"],
                "acceptanceDateTime": [
                    "2024-01-05T17:30:00-05:00",
                    "2024-01-06T12:00:00-05:00",
                ],
                "reportDate": ["2024-01-05", "2024-01-06"],
                "items": ["2.02,9.01", ""],
                "primaryDocument": ["nvda-20240105.htm", "ownership.xml"],
            }
        }
    }

    result = normalize_sec_submissions(payload, "NVDA", 1045810)

    assert len(result) == 1
    assert result.loc[0, "Form"] == "8-K"
    assert result.loc[0, "AcceptedAt"] == pd.Timestamp("2024-01-05T22:30:00Z")
    assert "000104581024000001" in result.loc[0, "Url"]


def test_normalize_sec_submissions_rejects_invalid_payload() -> None:
    with pytest.raises(TypeError, match="no filings object"):
        normalize_sec_submissions({}, "NVDA", 1045810)


def test_normalize_sec_submission_history_file() -> None:
    payload = {
        "accessionNumber": ["0001045810-18-000001"],
        "form": ["10-K"],
        "filingDate": ["2018-02-28"],
        "reportDate": ["2018-01-28"],
        "primaryDocument": ["nvda-20180128x10k.htm"],
    }

    result = normalize_sec_submissions(payload, "NVDA", 1045810)

    assert len(result) == 1
    assert result.loc[0, "Form"] == "10-K"
    assert result.loc[0, "AcceptedAt"] == pd.Timestamp("2018-03-01T04:59:59Z")
