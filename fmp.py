from __future__ import annotations

import io
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd
import requests


BASE_URL = "https://financialmodelingprep.com/stable"


class FMPError(RuntimeError):
    pass


@dataclass
class FMPClient:
    api_key: str
    timeout: int = 60

    @classmethod
    def from_env(cls) -> "FMPClient":
        key = os.getenv("FMP_API_KEY", "").strip()
        if not key:
            raise FMPError("FMP_API_KEY ist nicht gesetzt.")
        return cls(key)

    def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> requests.Response:
        p = dict(params or {})
        p["apikey"] = self.api_key
        r = requests.get(f"{BASE_URL}/{endpoint.lstrip('/')}", params=p, timeout=self.timeout)
        if not r.ok:
            raise FMPError(f"FMP HTTP {r.status_code}: {r.text[:300]}")
        return r

    def json(self, endpoint: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        r = self._get(endpoint, params)
        try:
            data = r.json()
        except Exception as exc:
            raise FMPError(f"Ungültige JSON-Antwort von {endpoint}") from exc
        if isinstance(data, dict) and ("Error Message" in data or "error" in data):
            raise FMPError(str(data))
        if not isinstance(data, list):
            raise FMPError(f"Unerwartetes Datenformat von {endpoint}: {type(data).__name__}")
        return data

    def dataframe(self, endpoint: str, params: dict[str, Any] | None = None) -> pd.DataFrame:
        """Read endpoint regardless of whether FMP returns JSON or CSV."""
        r = self._get(endpoint, params)
        content_type = (r.headers.get("content-type") or "").lower()
        text = r.text.lstrip()
        if "csv" in content_type or (text and not text.startswith("[") and not text.startswith("{")):
            try:
                return pd.read_csv(io.StringIO(r.text))
            except Exception:
                pass
        try:
            data = r.json()
        except Exception as exc:
            raise FMPError(f"Antwort von {endpoint} ist weder lesbares JSON noch CSV.") from exc
        if isinstance(data, dict):
            if "Error Message" in data or "error" in data:
                raise FMPError(str(data))
            data = [data]
        return pd.DataFrame(data)

    def key_metrics_ttm_bulk(self) -> pd.DataFrame:
        return self.dataframe("key-metrics-ttm-bulk")

    def ratios_ttm_bulk(self) -> pd.DataFrame:
        return self.dataframe("ratios-ttm-bulk")

    def income_growth_bulk(self, year: int) -> pd.DataFrame:
        return self.dataframe("income-statement-growth-bulk", {"year": year, "period": "FY"})

    def cashflow_growth_bulk(self, year: int) -> pd.DataFrame:
        return self.dataframe("cash-flow-statement-growth-bulk", {"year": year, "period": "FY"})

    def stock_list(self) -> pd.DataFrame:
        return self.dataframe("stock-list")

    def profile(self, symbol: str) -> pd.DataFrame:
        return self.dataframe("profile", {"symbol": symbol})

    def analyst_estimates(self, symbol: str, limit: int = 6) -> pd.DataFrame:
        return self.dataframe(
            "analyst-estimates",
            {"symbol": symbol, "period": "annual", "page": 0, "limit": limit},
        )

    def batch_quote(self, symbols: list[str] | tuple[str, ...], chunk_size: int = 100) -> pd.DataFrame:
        """Fetch current quote + 50/200-day averages for a manageable symbol set."""
        clean = [str(s).strip() for s in symbols if str(s).strip()]
        if not clean:
            return pd.DataFrame()
        frames: list[pd.DataFrame] = []
        for i in range(0, len(clean), chunk_size):
            chunk = clean[i:i + chunk_size]
            frame = self.dataframe("batch-quote", {"symbols": ",".join(chunk)})
            if not frame.empty:
                frames.append(frame)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True).drop_duplicates("symbol", keep="first")

    def stock_price_change(self, symbol: str) -> pd.DataFrame:
        return self.dataframe("stock-price-change", {"symbol": symbol})

    def price_target_consensus(self, symbol: str) -> pd.DataFrame:
        return self.dataframe("price-target-consensus", {"symbol": symbol})

    def grades_summary(self, symbol: str) -> pd.DataFrame:
        return self.dataframe("grades-consensus", {"symbol": symbol})

    def grades_historical(self, symbol: str, limit: int = 100) -> pd.DataFrame:
        return self.dataframe("grades-historical", {"symbol": symbol, "page": 0, "limit": limit})


def _coalesce(df: pd.DataFrame, target: str, candidates: list[str]) -> None:
    for c in candidates:
        if c in df.columns:
            df[target] = pd.to_numeric(df[c], errors="coerce")
            return
    df[target] = pd.NA


def _prepare_growth(df: pd.DataFrame, kind: str, year: int) -> pd.DataFrame:
    if df.empty or "symbol" not in df.columns:
        return pd.DataFrame(columns=["symbol"])
    out = df.copy()
    out = out.drop_duplicates("symbol", keep="first")
    if kind == "income":
        _coalesce(out, f"revenueGrowth_{year}", ["growthRevenue", "revenueGrowth"])
        _coalesce(out, f"epsGrowth_{year}", ["growthEPS", "growthEps", "epsGrowth"])
        _coalesce(
            out,
            f"sharesGrowth_{year}",
            ["growthWeightedAverageShsOutDil", "growthWeightedAverageShsOut", "growthWeightedAverageSharesDiluted", "growthWeightedAverageShares"],
        )
        return out[["symbol", f"revenueGrowth_{year}", f"epsGrowth_{year}", f"sharesGrowth_{year}"]]
    _coalesce(out, f"fcfGrowth_{year}", ["growthFreeCashFlow", "freeCashFlowGrowth"])
    return out[["symbol", f"fcfGrowth_{year}"]]


def _mean_available(df: pd.DataFrame, target: str, columns: list[str]) -> None:
    valid = [c for c in columns if c in df.columns]
    if not valid:
        df[target] = pd.NA
        return
    vals = df[valid].apply(pd.to_numeric, errors="coerce")
    # Winsorize absurd API artefacts before averaging.
    vals = vals.clip(lower=-1.0, upper=3.0)
    df[target] = vals.mean(axis=1, skipna=True)


def build_global_snapshot(client: FMPClient, annual_years: list[int] | None = None) -> pd.DataFrame:
    """Fetch and merge bulk datasets for the global quantitative screen."""
    if annual_years is None:
        current = datetime.utcnow().year
        annual_years = [current - 1, current - 2]

    metrics = client.key_metrics_ttm_bulk()
    ratios = client.ratios_ttm_bulk()
    if metrics.empty or ratios.empty:
        raise FMPError("TTM-Bulkdaten sind leer. Prüfe API-Key bzw. Tarifzugriff.")

    metrics = metrics.drop_duplicates("symbol", keep="first")
    ratios = ratios.drop_duplicates("symbol", keep="first")
    df = metrics.merge(ratios, on="symbol", how="inner", suffixes=("", "_ratio"))

    for year in annual_years:
        inc = _prepare_growth(client.income_growth_bulk(year), "income", year)
        cf = _prepare_growth(client.cashflow_growth_bulk(year), "cashflow", year)
        if not inc.empty:
            df = df.merge(inc, on="symbol", how="left")
        if not cf.empty:
            df = df.merge(cf, on="symbol", how="left")

    _mean_available(df, "revenueGrowth", [f"revenueGrowth_{y}" for y in annual_years])
    _mean_available(df, "epsGrowth", [f"epsGrowth_{y}" for y in annual_years])
    _mean_available(df, "sharesGrowth", [f"sharesGrowth_{y}" for y in annual_years])
    _mean_available(df, "fcfGrowth", [f"fcfGrowth_{y}" for y in annual_years])

    # Enrich names/exchanges cheaply from the stock directory. Sector/country are fetched on demand.
    try:
        directory = client.stock_list()
        keep = [c for c in ["symbol", "name", "exchange", "exchangeFullName", "currency"] if c in directory.columns]
        if "symbol" in keep:
            directory = directory[keep].drop_duplicates("symbol", keep="first")
            df = df.merge(directory, on="symbol", how="left")
    except Exception:
        pass

    return df
