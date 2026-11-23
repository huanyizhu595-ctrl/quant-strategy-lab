"""Download point-in-time SEC filing metadata for structured event risk."""

import json
from datetime import UTC, datetime

import pandas as pd
import requests

from src.mean_reversion_data import (
    RAW_DATA_DIR,
    START_DATE,
    TICKERS,
    load_required_env,
)

SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SEC_SUBMISSIONS_FILE_URL = "https://data.sec.gov/submissions/{name}"
SEC_ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
TICKER_CIK = {"NVDA": 1045810, "MU": 723125, "MSFT": 789019}
TRACKED_FORMS = {"8-K", "8-K/A", "10-Q", "10-Q/A", "10-K", "10-K/A"}

FILINGS_DIR = RAW_DATA_DIR / "filings"
FILINGS_PATH = FILINGS_DIR / "sec_filings.parquet"
FILINGS_METADATA_PATH = FILINGS_DIR / "sec_filings_metadata.json"


def _parse_sec_acceptance(value: object, fallback_date: object) -> pd.Timestamp:
    timestamp = pd.Timestamp(value if value else fallback_date)
    if not value:
        timestamp = timestamp.normalize() + pd.Timedelta(days=1, seconds=-1)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("America/New_York")
    return timestamp.tz_convert("UTC")


def normalize_sec_submissions(
    payload: dict[str, object],
    ticker: str,
    cik: int,
) -> pd.DataFrame:
    """Convert SEC recent-submission arrays into event records."""

    filings = payload.get("filings")
    if isinstance(filings, dict):
        recent = filings.get("recent")
    elif isinstance(payload.get("accessionNumber"), list):
        recent = payload
    else:
        raise TypeError(f"SEC response for {ticker} has no filings object")
    if not isinstance(recent, dict):
        raise TypeError(f"SEC response for {ticker} has no recent filings")

    accessions = recent.get("accessionNumber", [])
    if not isinstance(accessions, list):
        raise TypeError(f"SEC response for {ticker} has invalid accession data")

    def value_at(name: str, index: int, default: object = "") -> object:
        values = recent.get(name, [])
        if isinstance(values, list) and index < len(values):
            return values[index]
        return default

    rows: list[dict[str, object]] = []
    for index, accession in enumerate(accessions):
        form = str(value_at("form", index))
        if form not in TRACKED_FORMS:
            continue
        filing_date = value_at("filingDate", index)
        primary_document = str(value_at("primaryDocument", index))
        accession_compact = str(accession).replace("-", "")
        rows.append(
            {
                "Ticker": ticker,
                "CIK": cik,
                "AccessionNumber": str(accession),
                "Form": form,
                "FilingDate": pd.Timestamp(filing_date),
                "AcceptedAt": _parse_sec_acceptance(
                    value_at("acceptanceDateTime", index), filing_date
                ),
                "ReportDate": value_at("reportDate", index),
                "Items": str(value_at("items", index)),
                "PrimaryDocument": primary_document,
                "Url": SEC_ARCHIVES_URL.format(
                    cik=cik,
                    accession=accession_compact,
                    document=primary_document,
                ),
            }
        )

    return pd.DataFrame(rows)


def download_sec_filings() -> None:
    """Download official filing metadata using the required declared user agent."""

    user_agent = load_required_env("SEC_USER_AGENT")
    frames: list[pd.DataFrame] = []
    headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}

    with requests.Session() as session:
        for ticker in TICKERS:
            cik = TICKER_CIK[ticker]
            response = session.get(
                SEC_SUBMISSIONS_URL.format(cik=cik),
                headers=headers,
                timeout=60,
            )
            response.raise_for_status()
            payload = response.json()
            ticker_frames = [normalize_sec_submissions(payload, ticker, cik)]

            filings = payload.get("filings", {})
            history_files = filings.get("files", []) if isinstance(filings, dict) else []
            if isinstance(history_files, list):
                for history_file in history_files:
                    if not isinstance(history_file, dict):
                        continue
                    filing_to = history_file.get("filingTo")
                    if filing_to and pd.Timestamp(filing_to).date() < START_DATE:
                        continue
                    name = history_file.get("name")
                    if not name:
                        continue
                    history_response = session.get(
                        SEC_SUBMISSIONS_FILE_URL.format(name=name),
                        headers=headers,
                        timeout=60,
                    )
                    history_response.raise_for_status()
                    ticker_frames.append(
                        normalize_sec_submissions(
                            history_response.json(),
                            ticker,
                            cik,
                        )
                    )

            frame = pd.concat(ticker_frames, ignore_index=True)
            frame = frame.loc[frame["FilingDate"].dt.date >= START_DATE]
            frames.append(frame)
            print(f"SEC filings: {ticker} ({len(frame):,} tracked filings)")

    filings = pd.concat(frames, ignore_index=True)
    filings = filings.drop_duplicates(subset="AccessionNumber", keep="last")
    filings = filings.sort_values(["AcceptedAt", "Ticker"]).reset_index(drop=True)
    FILINGS_DIR.mkdir(parents=True, exist_ok=True)
    filings.to_parquet(FILINGS_PATH, index=False)

    metadata = {
        "provider": "SEC EDGAR Submissions API",
        "tickers": list(TICKERS),
        "forms": sorted(TRACKED_FORMS),
        "downloaded_at_utc": datetime.now(UTC).isoformat(),
        "filings": len(filings),
        "first_accepted_at": filings["AcceptedAt"].min().isoformat(),
        "last_accepted_at": filings["AcceptedAt"].max().isoformat(),
        "columns": filings.columns.tolist(),
    }
    FILINGS_METADATA_PATH.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"SEC filing archive updated: {FILINGS_PATH}")
    print(f"Metadata: {FILINGS_METADATA_PATH}")


if __name__ == "__main__":
    download_sec_filings()
