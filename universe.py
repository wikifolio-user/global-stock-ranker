from __future__ import annotations

import io
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

from settings import ACWI_HOLDINGS_URL


class UniverseError(RuntimeError):
    pass


# iShares exchange labels -> Yahoo/Finnhub-style suffixes.
# US listings intentionally have no suffix.
EXCHANGE_SUFFIXES = {
    "NASDAQ": "",
    "New York Stock Exchange Inc.": "",
    "NYSE": "",
    "NYSE Arca": "",
    "NYSE American": "",
    "Toronto Stock Exchange": ".TO",
    "TSX Venture Exchange": ".V",
    "London Stock Exchange": ".L",
    "Euronext Amsterdam": ".AS",
    "Euronext Paris": ".PA",
    "Euronext Brussels": ".BR",
    "Euronext Lisbon": ".LS",
    "Borsa Italiana": ".MI",
    "SIX Swiss Exchange": ".SW",
    "Deutsche Boerse Xetra": ".DE",
    "Xetra": ".DE",
    "Frankfurt Stock Exchange": ".F",
    "Bolsa De Madrid": ".MC",
    "Spanish Stock Exchange": ".MC",
    "Nasdaq Stockholm Ab": ".ST",
    "Stockholm Stock Exchange": ".ST",
    "Nasdaq Copenhagen A/S": ".CO",
    "Copenhagen Stock Exchange": ".CO",
    "Oslo Bors Asa": ".OL",
    "Oslo Stock Exchange": ".OL",
    "Nasdaq Helsinki Ltd.": ".HE",
    "Helsinki Stock Exchange": ".HE",
    "Warsaw Stock Exchange/Equities/Main Market": ".WA",
    "Warsaw Stock Exchange": ".WA",
    "Tokyo Stock Exchange": ".T",
    "Japan Exchange Group": ".T",
    "Hong Kong Exchanges And Clearing Ltd": ".HK",
    "Hong Kong Stock Exchange": ".HK",
    "Taiwan Stock Exchange": ".TW",
    "Taipei Exchange": ".TWO",
    "Korea Exchange (Stock Market)": ".KS",
    "Korea Exchange (Kosdaq)": ".KQ",
    "Australian Securities Exchange": ".AX",
    "New Zealand Exchange Ltd": ".NZ",
    "Singapore Exchange": ".SI",
    "National Stock Exchange Of India": ".NS",
    "Bse Ltd": ".BO",
    "Indonesia Stock Exchange": ".JK",
    "Bursa Malaysia": ".KL",
    "Stock Exchange Of Thailand": ".BK",
    "Johannesburg Stock Exchange": ".JO",
    "Tel Aviv Stock Exchange": ".TA",
    "Saudi Stock Exchange": ".SR",
    "Brazil Bolsa Balcao S.A": ".SA",
    "BM&FBovespa": ".SA",
    "Bolsa Mexicana De Valores": ".MX",
    "Santiago Stock Exchange": ".SN",
    "Borsa Istanbul": ".IS",
}


def _clean_text(value) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip().strip('"')


def normalize_us_ticker(value: str) -> str:
    """Normalize US class-share tickers for Yahoo/SEC comparisons."""
    s = _clean_text(value).upper()
    s = re.sub(r"\s+", "-", s)
    s = s.replace(".", "-")
    return s


def canonical_key(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", _clean_text(value).upper())


def to_provider_symbol(ticker: str, exchange: str, location: str = "") -> str:
    ticker = _clean_text(ticker)
    exchange = _clean_text(exchange)
    location = _clean_text(location)
    if not ticker:
        return ""

    # US share classes: BRK B -> BRK-B, BF B -> BF-B.
    if location == "United States" or exchange in {"NASDAQ", "NYSE", "New York Stock Exchange Inc.", "NYSE Arca", "NYSE American"}:
        return normalize_us_ticker(ticker)

    suffix = EXCHANGE_SUFFIXES.get(exchange, "")

    # Hong Kong commonly needs 4 digits in Yahoo-style symbology.
    if suffix == ".HK" and ticker.isdigit():
        ticker = ticker.zfill(4)

    # Preserve leading zeros for Korea/Taiwan/Japan.
    ticker = ticker.replace(" ", "-")
    if suffix:
        return f"{ticker}{suffix}"
    return ticker


def parse_ishares_holdings_csv(text: str) -> pd.DataFrame:
    """Parse the human-readable iShares holdings CSV.

    The file contains several metadata lines before the real header. We detect the
    header instead of relying on a fixed row number so the parser survives small
    website changes.
    """
    if not text or "Ticker" not in text:
        raise UniverseError("iShares-Holdings konnten nicht gelesen werden.")

    lines = text.splitlines()
    header_idx = None
    for i, line in enumerate(lines[:40]):
        if line.startswith("Ticker,") and "Asset Class" in line:
            header_idx = i
            break
    if header_idx is None:
        raise UniverseError("Unerwartetes iShares-CSV-Format: Tabellenkopf fehlt.")

    df = pd.read_csv(io.StringIO("\n".join(lines[header_idx:])))
    rename = {
        "Ticker": "symbol",
        "Name": "name",
        "Sector": "sector",
        "Asset Class": "asset_class",
        "Market Value": "fund_market_value",
        "Weight (%)": "ishares_weight_pct",
        "Quantity": "fund_quantity",
        "Price": "ishares_price_usd",
        "Location": "country",
        "Exchange": "exchange",
        "Currency": "fund_currency",
        "FX Rate": "fx_rate",
        "Market Currency": "currency",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
    if "symbol" not in df.columns:
        raise UniverseError("iShares-CSV enthält keine Ticker-Spalte.")

    for col in ["symbol", "name", "sector", "asset_class", "country", "exchange", "currency", "fund_currency"]:
        if col in df.columns:
            df[col] = df[col].map(_clean_text)

    # Keep only actual equities and usable tickers.
    if "asset_class" in df.columns:
        df = df[df["asset_class"].str.lower().eq("equity")]
    df = df[df["symbol"].astype(str).str.strip().ne("")]
    df = df[~df["symbol"].astype(str).str.upper().isin({"USD", "EUR", "GBP", "CASH"})]

    for col in ["fund_market_value", "ishares_weight_pct", "fund_quantity", "ishares_price_usd", "fx_rate"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col].astype(str).str.replace(",", "", regex=False), errors="coerce")

    if "country" not in df.columns:
        df["country"] = ""
    if "exchange" not in df.columns:
        df["exchange"] = ""

    df["provider_symbol"] = [
        to_provider_symbol(t, e, c)
        for t, e, c in zip(df["symbol"], df["exchange"], df["country"])
    ]
    df["canonical_key"] = df["symbol"].map(canonical_key)
    df["universe_source"] = "iShares ACWI"
    df["universe_updated_at"] = datetime.now(timezone.utc).isoformat()

    # One economic company can have multiple share classes; keep each listed security,
    # because valuation and liquidity can differ. Remove literal duplicate rows only.
    df = df.drop_duplicates(["symbol", "exchange"], keep="first").reset_index(drop=True)
    return df


def fetch_acwi_universe(timeout: int = 45) -> pd.DataFrame:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; GlobalStockRanker/3.0; personal research app)",
        "Accept": "text/csv,text/plain,*/*",
    }
    try:
        response = requests.get(ACWI_HOLDINGS_URL, headers=headers, timeout=timeout)
        response.raise_for_status()
    except Exception as exc:
        raise UniverseError(f"iShares-Universum konnte nicht geladen werden: {exc}") from exc
    return parse_ishares_holdings_csv(response.text)


def save_universe(df: pd.DataFrame, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, compression="gzip")


def load_universe_cache(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()
