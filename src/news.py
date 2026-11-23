"""Incrementally collect timestamped company news from Marketaux."""

import json
from datetime import UTC, datetime, timedelta

import pandas as pd
import requests

from src.data import RAW_DATA_DIR, TICKERS, load_required_env

MARKETAUX_URL = "https://api.marketaux.com/v1/news/all"
NEWS_DIR = RAW_DATA_DIR / "news"
NEWS_PATH = NEWS_DIR / "marketaux_news.parquet"
NEWS_METADATA_PATH = NEWS_DIR / "marketaux_news_metadata.json"


def normalize_marketaux_response(
    payload: dict[str, object],
    requested_ticker: str,
) -> pd.DataFrame:
    """Convert Marketaux articles into the event-risk news schema."""

    data = payload.get("data")
    if not isinstance(data, list):
        error = payload.get("error", {})
        raise RuntimeError(f"Marketaux error: {error}")  # noqa: TRY004

    rows: list[dict[str, object]] = []
    for article in data:
        if not isinstance(article, dict):
            continue
        entities = article.get("entities", [])
        symbols: list[str] = []
        sentiment_score: float | None = None
        if isinstance(entities, list):
            for entity in entities:
                if not isinstance(entity, dict):
                    continue
                symbol = entity.get("symbol")
                if symbol:
                    symbols.append(str(symbol).upper())
                if str(symbol).upper() == requested_ticker.upper():
                    raw_sentiment = entity.get("sentiment_score")
                    if raw_sentiment is not None:
                        sentiment_score = float(raw_sentiment)

        rows.append(
            {
                "ArticleId": str(article.get("uuid", "")),
                "CreatedAt": article.get("published_at"),
                "UpdatedAt": article.get("published_at"),
                "Headline": str(article.get("title", "")),
                "Summary": str(
                    article.get("description") or article.get("snippet") or ""
                ),
                "Symbols": sorted(set(symbols or [requested_ticker.upper()])),
                "Source": str(article.get("source", "")),
                "Author": "",
                "Url": article.get("url"),
                "SentimentScore": sentiment_score,
            }
        )

    result = pd.DataFrame(rows)
    if not result.empty:
        result["CreatedAt"] = pd.to_datetime(result["CreatedAt"], utc=True)
        result["UpdatedAt"] = pd.to_datetime(result["UpdatedAt"], utc=True)
    return result


def fetch_recent_marketaux_news(
    ticker: str,
    api_token: str,
    published_after: datetime,
    pages: int = 5,
    session: requests.Session | None = None,
) -> pd.DataFrame:
    """Fetch a bounded number of recent pages for one ticker."""

    http = session or requests.Session()
    frames: list[pd.DataFrame] = []
    for page in range(1, pages + 1):
        response = http.get(
            MARKETAUX_URL,
            params={
                "symbols": ticker,
                "filter_entities": "true",
                "group_similar": "true",
                "language": "en",
                "published_after": published_after.strftime("%Y-%m-%dT%H:%M:%S"),
                "limit": 3,
                "page": page,
                "api_token": api_token,
            },
            timeout=60,
        )
        response.raise_for_status()
        frame = normalize_marketaux_response(response.json(), ticker)
        if frame.empty:
            break
        frames.append(frame)
        if len(frame) < 3:
            break

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def download_news_data(lookback_days: int = 7, pages_per_ticker: int = 5) -> None:
    """Append recent Marketaux news to the local point-in-time archive."""

    if lookback_days <= 0 or pages_per_ticker <= 0:
        raise ValueError("lookback_days and pages_per_ticker must be positive")

    api_token = load_required_env("MARKETAUX_API_TOKEN")
    downloaded_at = datetime.now(UTC)
    published_after = downloaded_at - timedelta(days=lookback_days)
    frames: list[pd.DataFrame] = []

    with requests.Session() as session:
        for ticker in TICKERS:
            frame = fetch_recent_marketaux_news(
                ticker,
                api_token,
                published_after,
                pages_per_ticker,
                session,
            )
            if not frame.empty:
                frames.append(frame)
            print(f"Recent news: {ticker} ({len(frame):,} articles)")

    new_articles = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    existing = pd.read_parquet(NEWS_PATH) if NEWS_PATH.exists() else pd.DataFrame()
    news = pd.concat([existing, new_articles], ignore_index=True)
    if news.empty:
        raise RuntimeError("Marketaux returned no news for the configured tickers")

    news = news.drop_duplicates(subset="ArticleId", keep="last")
    news = news.sort_values(["CreatedAt", "ArticleId"]).reset_index(drop=True)
    NEWS_DIR.mkdir(parents=True, exist_ok=True)
    news.to_parquet(NEWS_PATH, index=False)

    metadata = {
        "provider": "Marketaux",
        "tickers": list(TICKERS),
        "collection_mode": "incremental recent news",
        "lookback_days": lookback_days,
        "pages_per_ticker": pages_per_ticker,
        "downloaded_at_utc": downloaded_at.isoformat(),
        "articles_in_archive": len(news),
        "first_created_at": news["CreatedAt"].min().isoformat(),
        "last_created_at": news["CreatedAt"].max().isoformat(),
        "columns": news.columns.tolist(),
    }
    NEWS_METADATA_PATH.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"News archive updated: {NEWS_PATH}")
    print(f"Metadata: {NEWS_METADATA_PATH}")


if __name__ == "__main__":
    download_news_data()
