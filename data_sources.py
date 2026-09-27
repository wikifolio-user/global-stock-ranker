from __future__ import annotations

import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import quote

import numpy as np
import pandas as pd
import requests

from settings import (
    FINNHUB_BASE_URL,
    FINNHUB_DELAY_SECONDS,
    FINNHUB_RETRY_AFTER_SECONDS,
    SEC_BASE_URL,
    SEC_REQUEST_DELAY_SECONDS,
    SEC_SCREEN_CURRENT_YEAR_OFFSET,
    SEC_SCREEN_GROWTH_YEARS,
    SEC_WWW_BASE_URL,
    YAHOO_PRICE_BATCH,
    YAHOO_PRICE_PERIOD,
    YAHOO_PRICE_PAUSE_SECONDS,
    YAHOO_PRICE_WORKERS,
    YAHOO_WORKERS,
)
from universe import canonical_key


class DataSourceError(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _float(v: Any) -> float:
    try:
        x = float(v)
        if math.isfinite(x):
            return x
    except Exception:
        pass
    return np.nan


def _fraction(v: Any) -> float:
    """Normalize percentage-like vendor fields to decimal fractions."""
    x = _float(v)
    if pd.isna(x):
        return np.nan
    # Finnhub commonly returns margins/growth in percentage points.
    if abs(x) > 1.5:
        return x / 100.0
    return x


def _cagr(new: Any, old: Any, years: int) -> float:
    n, o = _float(new), _float(old)
    if years <= 0 or pd.isna(n) or pd.isna(o) or o <= 0 or n <= 0:
        return np.nan
    try:
        return (n / o) ** (1.0 / years) - 1.0
    except Exception:
        return np.nan


def _safe_div(a: Any, b: Any) -> float:
    a, b = _float(a), _float(b)
    if pd.isna(a) or pd.isna(b) or b == 0:
        return np.nan
    return a / b


# ---------------------------------------------------------------------------
# Yahoo / yfinance: global price history + best-effort fundamentals
# ---------------------------------------------------------------------------

def _import_yfinance():
    try:
        import yfinance as yf  # type: ignore
    except Exception as exc:
        raise DataSourceError("yfinance ist nicht installiert. requirements.txt prüfen.") from exc
    return yf


def _history_snapshot(sub: pd.DataFrame) -> dict[str, float]:
    if sub is None or sub.empty:
        return {}
    close_col = "Close" if "Close" in sub.columns else ("Adj Close" if "Adj Close" in sub.columns else None)
    if close_col is None:
        return {}
    close = pd.to_numeric(sub[close_col], errors="coerce").dropna()
    if close.empty:
        return {}
    close = close.tail(260)
    last = float(close.iloc[-1])

    def ret(periods: int) -> float:
        if len(close) <= periods:
            if len(close) < 2:
                return np.nan
            base = float(close.iloc[0])
        else:
            base = float(close.iloc[-periods - 1])
        return np.nan if base <= 0 else last / base - 1.0

    high = pd.to_numeric(sub.get("High"), errors="coerce").dropna().tail(252) if "High" in sub.columns else close
    low = pd.to_numeric(sub.get("Low"), errors="coerce").dropna().tail(252) if "Low" in sub.columns else close
    volume = pd.to_numeric(sub.get("Volume"), errors="coerce").dropna() if "Volume" in sub.columns else pd.Series(dtype=float)
    prev = float(close.iloc[-2]) if len(close) >= 2 else np.nan

    return {
        "price": last,
        "priceAvg50": float(close.tail(50).mean()) if len(close) >= 20 else np.nan,
        "priceAvg200": float(close.tail(200).mean()) if len(close) >= 120 else np.nan,
        "yearHigh": float(high.max()) if not high.empty else np.nan,
        "yearLow": float(low.min()) if not low.empty else np.nan,
        "changePercentage": (last / prev - 1.0) * 100 if pd.notna(prev) and prev > 0 else np.nan,
        "return1m": ret(21),
        "return3m": ret(63),
        "return6m": ret(126),
        "return12m": ret(252),
        "volume": float(volume.iloc[-1]) if not volume.empty else np.nan,
        "avgVolume20": float(volume.tail(20).mean()) if not volume.empty else np.nan,
    }


def _yahoo_chart_row(symbol: str, provider_symbol: str, period: str = YAHOO_PRICE_PERIOD, timeout: int = 20) -> dict[str, Any]:
    """Fetch OHLCV from Yahoo's chart endpoint without quoteSummary/crumb auth.

    The v8 chart endpoint does not require the crumb handshake used by Yahoo's
    quoteSummary endpoints. This keeps price/trend refreshes independent from the
    frequent 401 ``Invalid Crumb`` failures seen on Streamlit Cloud.
    """
    out: dict[str, Any] = {
        "symbol": symbol,
        "provider_symbol": provider_symbol,
        "price_source": "YAHOO_CHART",
        "price_updated_at": _now_iso(),
    }
    if not provider_symbol:
        out["provider_error"] = "EMPTY_SYMBOL"
        return out

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(str(provider_symbol), safe='')}"
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; GlobalStockRanker/3.2; personal research app)",
        "Accept": "application/json,text/plain,*/*",
    }
    params = {
        "range": period,
        "interval": "1d",
        "events": "div,splits",
        "includeAdjustedClose": "true",
    }
    try:
        r = requests.get(url, params=params, headers=headers, timeout=timeout)
    except Exception as exc:
        out["provider_error"] = f"NETWORK: {str(exc)[:160]}"
        return out

    if r.status_code == 429:
        out["provider_error"] = "RATE_LIMIT"
        return out
    if r.status_code == 401:
        out["provider_error"] = "AUTH_401"
        return out
    if r.status_code == 404:
        out["provider_error"] = "NOT_FOUND"
        return out
    if not r.ok:
        out["provider_error"] = f"HTTP_{r.status_code}"
        return out

    try:
        payload = r.json()
        chart = payload.get("chart") or {}
        if chart.get("error"):
            err = chart.get("error") or {}
            out["provider_error"] = str(err.get("code") or err.get("description") or "CHART_ERROR")[:180]
            return out
        result = (chart.get("result") or [None])[0]
        if not isinstance(result, dict):
            out["provider_error"] = "NO_CHART_RESULT"
            return out
        timestamps = result.get("timestamp") or []
        indicators = result.get("indicators") or {}
        quote_block = (indicators.get("quote") or [{}])[0] or {}
        if not timestamps or not quote_block:
            out["provider_error"] = "NO_PRICE_DATA"
            return out

        frame = pd.DataFrame({
            "Close": quote_block.get("close", []),
            "High": quote_block.get("high", []),
            "Low": quote_block.get("low", []),
            "Volume": quote_block.get("volume", []),
        })
        adj_blocks = indicators.get("adjclose") or []
        if adj_blocks and isinstance(adj_blocks[0], dict):
            adj = adj_blocks[0].get("adjclose") or []
            if len(adj) == len(frame):
                frame["Close"] = pd.Series(adj).combine_first(frame["Close"])
        values = _history_snapshot(frame)
        if not values:
            out["provider_error"] = "NO_USABLE_PRICE_DATA"
            return out
        out.update(values)
        out["provider_error"] = ""
        return out
    except Exception as exc:
        out["provider_error"] = f"PARSE: {str(exc)[:160]}"
        return out


def fetch_yahoo_price_snapshot(
    universe: pd.DataFrame,
    symbols: Iterable[str] | None = None,
    period: str = YAHOO_PRICE_PERIOD,
    batch_size: int = YAHOO_PRICE_BATCH,
) -> pd.DataFrame:
    """Fetch global price history via Yahoo chart API with conservative throttling.

    Version 3.2 avoids ``yf.download`` for core price refreshes because cloud IPs can
    be throttled after quoteSummary/crumb failures. Requests are made in small
    batches, old cache rows remain intact on failures, and a rate limit stops the run
    instead of hammering Yahoo.
    """
    work = universe[["symbol", "provider_symbol"]].dropna().drop_duplicates("symbol").copy()
    if symbols is not None:
        wanted = {str(s) for s in symbols}
        work = work[work["symbol"].astype(str).isin(wanted)]
    work = work[work["provider_symbol"].astype(str).str.len() > 0]
    if work.empty:
        return pd.DataFrame()

    records = work.to_dict("records")
    rows: list[dict[str, Any]] = []
    batch_size = max(1, int(batch_size))
    workers = max(1, int(YAHOO_PRICE_WORKERS))

    for start in range(0, len(records), batch_size):
        batch = records[start:start + batch_size]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_yahoo_chart_row, str(r["symbol"]), str(r["provider_symbol"]), period): r
                for r in batch
            }
            batch_rows = []
            for future in as_completed(futures):
                rec = futures[future]
                try:
                    batch_rows.append(future.result())
                except Exception as exc:
                    batch_rows.append({
                        "symbol": rec["symbol"],
                        "provider_symbol": rec["provider_symbol"],
                        "price_source": "YAHOO_CHART",
                        "price_updated_at": _now_iso(),
                        "provider_error": f"WORKER: {str(exc)[:160]}",
                    })
        rows.extend(batch_rows)
        # A single 429 is enough to stop. Keep previous cached prices rather than
        # producing hundreds of noisy failures from the same cloud IP.
        if any(str(r.get("provider_error", "")) == "RATE_LIMIT" for r in batch_rows):
            break
        if start + batch_size < len(records):
            time.sleep(max(0.0, float(YAHOO_PRICE_PAUSE_SECONDS)))

    return pd.DataFrame(rows)


def _statement_row(df: pd.DataFrame, candidates: list[str]) -> pd.Series:
    if df is None or df.empty:
        return pd.Series(dtype=float)
    for name in candidates:
        if name in df.index:
            return pd.to_numeric(df.loc[name], errors="coerce").dropna()
    return pd.Series(dtype=float)


def _growth_from_series(s: pd.Series, years: int = 3) -> float:
    if s.empty or len(s) < 2:
        return np.nan
    # yfinance columns are generally newest -> oldest.
    vals = pd.to_numeric(s, errors="coerce").dropna()
    if len(vals) < 2:
        return np.nan
    newest = float(vals.iloc[0])
    idx = min(years, len(vals) - 1)
    oldest = float(vals.iloc[idx])
    return _cagr(newest, oldest, idx)


def fetch_yahoo_fundamental(symbol: str, provider_symbol: str, deep: bool = False) -> dict[str, Any]:
    yf = _import_yfinance()
    out: dict[str, Any] = {
        "symbol": symbol,
        "provider_symbol": provider_symbol,
        "fundamental_source": "YAHOO",
        "fundamental_updated_at": _now_iso(),
    }
    try:
        ticker = yf.Ticker(provider_symbol)
        info = ticker.get_info() or {}
    except Exception as exc:
        out["provider_error"] = str(exc)[:220]
        return out

    market_cap = _float(info.get("marketCap"))
    free_cf = _float(info.get("freeCashflow"))
    op_cf = _float(info.get("operatingCashflow"))
    debt = _float(info.get("totalDebt"))
    cash = _float(info.get("totalCash"))
    ebitda = _float(info.get("ebitda"))
    net_income = _float(info.get("netIncomeToCommon"))
    revenue = _float(info.get("totalRevenue"))
    shares = _float(info.get("sharesOutstanding"))

    out.update({
        "name": info.get("shortName") or info.get("longName"),
        "sector": info.get("sector"),
        "country": info.get("country"),
        "currency": info.get("currency"),
        "marketCap": market_cap,
        "grossProfitMarginTTM": _fraction(info.get("grossMargins")),
        "operatingProfitMarginTTM": _fraction(info.get("operatingMargins")),
        "returnOnEquityTTM": _fraction(info.get("returnOnEquity")),
        "returnOnAssetsTTM": _fraction(info.get("returnOnAssets")),
        "incomeQualityTTM": _safe_div(op_cf, net_income),
        "freeCashFlowOperatingCashFlowRatioTTM": _safe_div(free_cf, op_cf),
        "freeCashFlowYieldTTM": _safe_div(free_cf, market_cap),
        "netDebtToEBITDATTM": _safe_div(debt - cash, ebitda),
        "currentRatioTTM": _float(info.get("currentRatio")),
        "priceToEarningsRatioTTM": _float(info.get("trailingPE")),
        "forwardPERatio": _float(info.get("forwardPE")),
        "forwardPriceToEarningsGrowthRatioTTM": _float(info.get("pegRatio")),
        "enterpriseValueMultipleTTM": _float(info.get("enterpriseToEbitda")),
        "revenuePerShareTTM": _float(info.get("revenuePerShare")) if pd.notna(_float(info.get("revenuePerShare"))) else _safe_div(revenue, shares),
        "freeCashFlowPerShareTTM": _safe_div(free_cf, shares),
        "revenueGrowth": _fraction(info.get("revenueGrowth")),
        "epsGrowth": _fraction(info.get("earningsGrowth")) if pd.notna(_fraction(info.get("earningsGrowth"))) else _fraction(info.get("earningsQuarterlyGrowth")),
        "beta": _float(info.get("beta")),
        "targetMeanPrice": _float(info.get("targetMeanPrice")),
        "targetHighPrice": _float(info.get("targetHighPrice")),
        "targetLowPrice": _float(info.get("targetLowPrice")),
        "analystCount": _float(info.get("numberOfAnalystOpinions")),
        "recommendationMean": _float(info.get("recommendationMean")),
    })

    if not deep:
        return out

    try:
        inc = ticker.get_income_stmt(freq="yearly")
    except Exception:
        inc = pd.DataFrame()
    try:
        cf = ticker.get_cash_flow(freq="yearly")
    except Exception:
        cf = pd.DataFrame()
    try:
        bs = ticker.get_balance_sheet(freq="yearly")
    except Exception:
        bs = pd.DataFrame()

    revenue_s = _statement_row(inc, ["Total Revenue", "Operating Revenue"])
    eps_s = _statement_row(inc, ["Diluted EPS", "Basic EPS"])
    fcf_s = _statement_row(cf, ["Free Cash Flow"])
    if fcf_s.empty:
        ocf_s = _statement_row(cf, ["Operating Cash Flow", "Total Cash From Operating Activities"])
        capex_s = _statement_row(cf, ["Capital Expenditure", "Capital Expenditures"])
        if not ocf_s.empty and not capex_s.empty:
            fcf_s = ocf_s.add(capex_s, fill_value=np.nan)  # yfinance capex is often negative
    shares_s = _statement_row(bs, ["Ordinary Shares Number", "Share Issued"])
    sbc_s = _statement_row(cf, ["Stock Based Compensation"])
    capex_s = _statement_row(cf, ["Capital Expenditure", "Capital Expenditures"])

    if pd.isna(out.get("revenueGrowth")):
        out["revenueGrowth"] = _growth_from_series(revenue_s)
    if pd.isna(out.get("epsGrowth")):
        out["epsGrowth"] = _growth_from_series(eps_s)
    out["fcfGrowth"] = _growth_from_series(fcf_s)
    out["sharesGrowth"] = _growth_from_series(shares_s)
    if not revenue_s.empty:
        latest_rev = _float(revenue_s.iloc[0])
        if not sbc_s.empty:
            out["stockBasedCompensationToRevenueTTM"] = _safe_div(abs(_float(sbc_s.iloc[0])), latest_rev)
        if not capex_s.empty:
            out["capexToRevenueTTM"] = _safe_div(abs(_float(capex_s.iloc[0])), latest_rev)
    return out


def enrich_yahoo_fundamentals(
    universe: pd.DataFrame,
    symbols: Iterable[str],
    deep: bool = False,
    workers: int = YAHOO_WORKERS,
) -> pd.DataFrame:
    wanted = {str(s) for s in symbols}
    work = universe[universe["symbol"].astype(str).isin(wanted)][["symbol", "provider_symbol"]].drop_duplicates("symbol")
    if work.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        futures = {
            pool.submit(fetch_yahoo_fundamental, r.symbol, r.provider_symbol, deep): r.symbol
            for r in work.itertuples(index=False)
            if str(r.provider_symbol)
        }
        for future in as_completed(futures):
            try:
                rows.append(future.result())
            except Exception as exc:
                rows.append({"symbol": futures[future], "provider_error": str(exc)[:220], "fundamental_source": "YAHOO", "fundamental_updated_at": _now_iso()})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Finnhub Basic Financials: free key, global ratios/metrics
# ---------------------------------------------------------------------------

def _metric(metric: dict[str, Any], *names: str) -> float:
    for name in names:
        if name in metric and metric[name] is not None:
            x = _float(metric[name])
            if pd.notna(x):
                return x
    return np.nan


def _metric_fraction(metric: dict[str, Any], *names: str) -> float:
    return _fraction(_metric(metric, *names))


def _annual_series_values(series: dict[str, Any], candidates: list[str]) -> list[float]:
    annual = (series or {}).get("annual", {}) if isinstance(series, dict) else {}
    if not isinstance(annual, dict):
        return []
    lower = {str(k).lower(): k for k in annual.keys()}
    selected = None
    for cand in candidates:
        if cand.lower() in lower:
            selected = annual.get(lower[cand.lower()])
            break
    if not isinstance(selected, list):
        return []
    vals = []
    for item in selected:
        if isinstance(item, dict):
            v = _float(item.get("v"))
            if pd.notna(v) and v > 0:
                vals.append(v)
    return vals[:5]


def fetch_finnhub_basic(symbol: str, provider_symbol: str, api_key: str, timeout: int = 25) -> dict[str, Any]:
    out: dict[str, Any] = {
        "symbol": symbol,
        "provider_symbol": provider_symbol,
        "fundamental_source": "FINNHUB",
        "fundamental_updated_at": _now_iso(),
    }
    try:
        r = requests.get(
            f"{FINNHUB_BASE_URL}/stock/metric",
            params={"symbol": provider_symbol, "metric": "all", "token": api_key},
            timeout=timeout,
        )
    except Exception as exc:
        out["provider_error"] = str(exc)[:220]
        return out
    if r.status_code == 429:
        out["provider_error"] = "RATE_LIMIT"
        return out
    if not r.ok:
        out["provider_error"] = f"HTTP {r.status_code}: {r.text[:150]}"
        return out
    try:
        payload = r.json()
    except Exception:
        out["provider_error"] = "Ungültige JSON-Antwort"
        return out
    metric = payload.get("metric") or {}
    series = payload.get("series") or {}
    if not metric:
        out["provider_error"] = "Keine Basic-Financials für Symbol"
        return out

    pfcf = _metric(metric, "priceToFreeCashFlowTTM", "pfcfTTM", "priceToFreeCashFlowAnnual")
    fcf_yield = 1.0 / pfcf if pd.notna(pfcf) and pfcf > 0 else np.nan
    cashflow_ps = _metric(metric, "cashFlowPerShareTTM", "cashFlowPerShareAnnual")
    eps = _metric(metric, "epsTTM", "epsBasicExclExtraItemsTTM")
    income_quality = _safe_div(cashflow_ps, eps)

    out.update({
        "returnOnInvestedCapitalTTM": _metric_fraction(metric, "returnOnInvestmentTTM", "roiTTM", "returnOnInvestmentAnnual"),
        "returnOnEquityTTM": _metric_fraction(metric, "roeTTM", "returnOnEquityTTM", "returnOnEquityAnnual"),
        "grossProfitMarginTTM": _metric_fraction(metric, "grossMarginTTM", "grossMarginAnnual"),
        "operatingProfitMarginTTM": _metric_fraction(metric, "operatingMarginTTM", "operatingMarginAnnual"),
        "incomeQualityTTM": income_quality,
        "freeCashFlowYieldTTM": fcf_yield,
        "netDebtToEBITDATTM": _metric(metric, "netDebt/ebitdaTTM", "netDebtToEbitdaTTM", "netDebt/ebitdaAnnual"),
        "currentRatioTTM": _metric(metric, "currentRatioTTM", "currentRatioAnnual"),
        "interestCoverageRatioTTM": _metric(metric, "interestCoverageTTM", "interestCoverageAnnual"),
        "priceToEarningsRatioTTM": _metric(metric, "peBasicExclExtraTTM", "peExclExtraTTM", "peTTM"),
        "forwardPriceToEarningsGrowthRatioTTM": _metric(metric, "pegTTM", "pegAnnual"),
        "enterpriseValueMultipleTTM": _metric(metric, "ev/ebitdaTTM", "evToEbitdaTTM", "ev/ebitdaAnnual"),
        "capexToRevenueTTM": _metric_fraction(metric, "capexToRevenueTTM", "capexToRevenueAnnual"),
        "revenuePerShareTTM": _metric(metric, "revenuePerShareTTM", "revenuePerShareAnnual"),
        "freeCashFlowPerShareTTM": _metric(metric, "freeCashFlowPerShareTTM", "freeCashFlowPerShareAnnual"),
        "revenueGrowth": _metric_fraction(metric, "revenueGrowthTTMYoy", "revenueGrowthQuarterlyYoy"),
        "epsGrowth": _metric_fraction(metric, "epsGrowthTTMYoy", "epsGrowthQuarterlyYoy"),
        "fcfGrowth": _metric_fraction(metric, "freeCashFlowGrowthTTMYoy", "freeCashFlowGrowth3Y", "freeCashFlowGrowth5Y"),
        "sharesGrowth": _metric_fraction(metric, "sharesOutstandingGrowthTTMYoy", "sharesOutstandingGrowth3Y", "sharesOutstandingGrowth5Y"),
        "beta": _metric(metric, "beta"),
    })

    # Some accounts expose FCF margin directly; use it when present.
    out["fcfMarginTTM"] = _metric_fraction(metric, "freeCashFlowMarginTTM", "fcfMarginTTM")

    # Historical valuation context: current PE vs up to five annual observations.
    hist_pe = _annual_series_values(series, ["pe", "peBasicExclExtra", "peExclExtra", "priceToEarnings"])
    current_pe = _float(out.get("priceToEarningsRatioTTM"))
    if len(hist_pe) >= 3 and pd.notna(current_pe):
        mean = float(np.mean(hist_pe))
        std = float(np.std(hist_pe, ddof=0))
        out["pe_5y_mean"] = mean
        out["pe_5y_median"] = float(np.median(hist_pe))
        out["valuation_zscore"] = (current_pe - mean) / std if std > 1e-9 else np.nan
    return out


def enrich_finnhub_fundamentals(
    universe: pd.DataFrame,
    symbols: Iterable[str],
    api_key: str,
    delay_seconds: float = FINNHUB_DELAY_SECONDS,
) -> pd.DataFrame:
    if not api_key:
        raise DataSourceError("FINNHUB_API_KEY fehlt.")
    wanted = {str(s) for s in symbols}
    work = universe[universe["symbol"].astype(str).isin(wanted)][["symbol", "provider_symbol"]].drop_duplicates("symbol")
    rows: list[dict[str, Any]] = []
    for rec in work.itertuples(index=False):
        row = fetch_finnhub_basic(str(rec.symbol), str(rec.provider_symbol), api_key)
        rows.append(row)
        if row.get("provider_error") == "RATE_LIMIT":
            # Stop instead of hammering the free endpoint. Existing rows are preserved.
            break
        time.sleep(max(0.0, float(delay_seconds)))
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# SEC EDGAR bulk frames: official US fundamentals, no API key
# ---------------------------------------------------------------------------

def _sec_get_json(url: str, user_agent: str, timeout: int = 35) -> dict[str, Any]:
    if not user_agent or "@" not in user_agent:
        raise DataSourceError(
            "Für SEC EDGAR bitte SEC_USER_AGENT mit Name + E-Mail setzen, z. B. 'Max Mustermann max@example.com'."
        )
    headers = {
        "User-Agent": user_agent,
        "Accept-Encoding": "gzip, deflate",
        "Host": url.split("/")[2],
    }
    r = requests.get(url, headers=headers, timeout=timeout)
    if not r.ok:
        raise DataSourceError(f"SEC HTTP {r.status_code} für {url.split('/')[-1]}")
    time.sleep(SEC_REQUEST_DELAY_SECONDS)
    return r.json()


def fetch_sec_ticker_map(user_agent: str) -> pd.DataFrame:
    payload = _sec_get_json(f"{SEC_WWW_BASE_URL}/files/company_tickers.json", user_agent)
    rows = list(payload.values()) if isinstance(payload, dict) else []
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["cik"] = pd.to_numeric(df.get("cik_str"), errors="coerce").astype("Int64")
    df["sec_ticker"] = df.get("ticker", "").astype(str)
    df["canonical_key"] = df["sec_ticker"].map(canonical_key)
    return df[["cik", "sec_ticker", "title", "canonical_key"]].dropna(subset=["cik"]).drop_duplicates("canonical_key")


def _sec_frame(tag: str, unit: str, frame: str, user_agent: str) -> pd.DataFrame:
    url = f"{SEC_BASE_URL}/api/xbrl/frames/us-gaap/{tag}/{unit}/{frame}.json"
    try:
        payload = _sec_get_json(url, user_agent)
    except Exception:
        return pd.DataFrame()
    data = payload.get("data") or []
    if not data:
        return pd.DataFrame()
    df = pd.DataFrame(data)
    if "cik" not in df.columns or "val" not in df.columns:
        return pd.DataFrame()
    df["cik"] = pd.to_numeric(df["cik"], errors="coerce").astype("Int64")
    df["val"] = pd.to_numeric(df["val"], errors="coerce")
    for c in ["filed", "end"]:
        if c not in df.columns:
            df[c] = ""
    df = df.sort_values(["cik", "filed", "end"]).drop_duplicates("cik", keep="last")
    return df[["cik", "val", "filed", "end"]]


def _collect_duration(tags: list[str], year: int, user_agent: str, unit: str = "USD") -> pd.DataFrame:
    result = pd.DataFrame(columns=["cik", "val"])
    for tag in tags:
        f = _sec_frame(tag, unit, f"CY{year}", user_agent)
        if f.empty:
            continue
        f = f[["cik", "val"]].rename(columns={"val": tag})
        if result.empty:
            result = f
        else:
            result = result.merge(f, on="cik", how="outer")
    if result.empty:
        return pd.DataFrame(columns=["cik", "value"])
    value_cols = [c for c in result.columns if c != "cik"]
    result["value"] = result[value_cols].bfill(axis=1).iloc[:, 0]
    return result[["cik", "value"]]


def _collect_instant(tags: list[str], year: int, user_agent: str, unit: str = "USD") -> pd.DataFrame:
    tag_frames: list[pd.DataFrame] = []
    for tag in tags:
        quarterly: list[pd.DataFrame] = []
        for q in range(1, 5):
            f = _sec_frame(tag, unit, f"CY{year}Q{q}I", user_agent)
            if not f.empty:
                f["tag"] = tag
                quarterly.append(f)
        if quarterly:
            cat = pd.concat(quarterly, ignore_index=True)
            cat = cat.sort_values(["cik", "end", "filed"]).drop_duplicates("cik", keep="last")
            tag_frames.append(cat[["cik", "val"]].rename(columns={"val": tag}))
    if not tag_frames:
        return pd.DataFrame(columns=["cik", "value"])
    result = tag_frames[0]
    for f in tag_frames[1:]:
        result = result.merge(f, on="cik", how="outer")
    value_cols = [c for c in result.columns if c != "cik"]
    result["value"] = result[value_cols].bfill(axis=1).iloc[:, 0]
    return result[["cik", "value"]]


def _merge_metric(base: pd.DataFrame, metric_df: pd.DataFrame, name: str) -> pd.DataFrame:
    if metric_df.empty:
        base[name] = np.nan
        return base
    return base.merge(metric_df.rename(columns={"value": name}), on="cik", how="left")


def fetch_sec_bulk_snapshot(
    universe: pd.DataFrame,
    user_agent: str,
    current_year: int | None = None,
    growth_years: int = SEC_SCREEN_GROWTH_YEARS,
) -> pd.DataFrame:
    """Build a fast US screening snapshot from SEC XBRL frames.

    This is intentionally a *screening* dataset: it uses annual/instant frames in bulk,
    then calculates common ratios consistently. A selected company can later be deep-
    checked against its filing if desired.
    """
    if current_year is None:
        current_year = datetime.now(timezone.utc).year - SEC_SCREEN_CURRENT_YEAR_OFFSET
    old_year = current_year - int(growth_years)

    us = universe[universe.get("country", pd.Series(index=universe.index, dtype=str)).astype(str).eq("United States")].copy()
    if us.empty:
        return pd.DataFrame()
    us["canonical_key"] = us["symbol"].map(canonical_key)
    ticker_map = fetch_sec_ticker_map(user_agent)
    us = us.merge(ticker_map[["cik", "canonical_key"]], on="canonical_key", how="left")
    us = us.dropna(subset=["cik"]).copy()
    us["cik"] = us["cik"].astype("Int64")
    base = us[["symbol", "cik", "ishares_price_usd"]].drop_duplicates("cik").copy()

    current_duration = {
        "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"],
        "net_income": ["NetIncomeLoss"],
        "operating_income": ["OperatingIncomeLoss"],
        "gross_profit": ["GrossProfit"],
        "operating_cashflow": ["NetCashProvidedByUsedInOperatingActivities"],
        "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsForAdditionsToPropertyPlantAndEquipment"],
        "pretax_income": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest", "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
        "tax_expense": ["IncomeTaxExpenseBenefit"],
        "interest_expense": ["InterestExpenseNonOperating", "InterestExpense"],
        "depreciation": ["DepreciationDepletionAndAmortization", "DepreciationDepletionAndAmortizationPropertyPlantAndEquipment"],
        "stock_comp": ["ShareBasedCompensation"],
    }
    old_duration = {
        "revenue_old": current_duration["revenue"],
        "net_income_old": current_duration["net_income"],
        "operating_cashflow_old": current_duration["operating_cashflow"],
        "capex_old": current_duration["capex"],
    }
    instant_current = {
        "cash": ["CashAndCashEquivalentsAtCarryingValue"],
        "current_assets": ["AssetsCurrent"],
        "current_liabilities": ["LiabilitiesCurrent"],
        "equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
        "debt_current": ["LongTermDebtCurrent", "LongTermDebtAndFinanceLeaseObligationsCurrent"],
        "debt_noncurrent": ["LongTermDebtNoncurrent", "LongTermDebtAndFinanceLeaseObligationsNoncurrent"],
        "shares": ["CommonStockSharesOutstanding"],
    }

    for name, tags in current_duration.items():
        base = _merge_metric(base, _collect_duration(tags, current_year, user_agent), name)
    for name, tags in old_duration.items():
        base = _merge_metric(base, _collect_duration(tags, old_year, user_agent), name)
    for name, tags in instant_current.items():
        unit = "shares" if name == "shares" else "USD"
        base = _merge_metric(base, _collect_instant(tags, current_year, user_agent, unit=unit), name)
    base = _merge_metric(base, _collect_instant(["CommonStockSharesOutstanding"], old_year, user_agent, unit="shares"), "shares_old")

    # Derived financials and ratios.
    base["capex"] = pd.to_numeric(base["capex"], errors="coerce").abs()
    base["capex_old"] = pd.to_numeric(base["capex_old"], errors="coerce").abs()
    base["free_cashflow"] = pd.to_numeric(base["operating_cashflow"], errors="coerce") - base["capex"]
    base["free_cashflow_old"] = pd.to_numeric(base["operating_cashflow_old"], errors="coerce") - base["capex_old"]
    base["debt"] = pd.to_numeric(base["debt_current"], errors="coerce").fillna(0) + pd.to_numeric(base["debt_noncurrent"], errors="coerce").fillna(0)
    base["ebitda"] = pd.to_numeric(base["operating_income"], errors="coerce") + pd.to_numeric(base["depreciation"], errors="coerce").fillna(0)
    base["marketCap"] = pd.to_numeric(base["ishares_price_usd"], errors="coerce") * pd.to_numeric(base["shares"], errors="coerce")
    base["enterprise_value"] = base["marketCap"] + base["debt"] - pd.to_numeric(base["cash"], errors="coerce").fillna(0)

    tax_rate = pd.to_numeric(base["tax_expense"], errors="coerce") / pd.to_numeric(base["pretax_income"], errors="coerce")
    tax_rate = tax_rate.where((tax_rate >= 0) & (tax_rate <= 0.40), 0.21)
    nopat = pd.to_numeric(base["operating_income"], errors="coerce") * (1 - tax_rate)
    invested_capital = pd.to_numeric(base["equity"], errors="coerce") + base["debt"] - pd.to_numeric(base["cash"], errors="coerce").fillna(0)

    result = pd.DataFrame({
        "symbol": base["symbol"],
        "marketCap": base["marketCap"],
        "returnOnInvestedCapitalTTM": nopat / invested_capital.replace(0, np.nan),
        "grossProfitMarginTTM": pd.to_numeric(base["gross_profit"], errors="coerce") / pd.to_numeric(base["revenue"], errors="coerce").replace(0, np.nan),
        "operatingProfitMarginTTM": pd.to_numeric(base["operating_income"], errors="coerce") / pd.to_numeric(base["revenue"], errors="coerce").replace(0, np.nan),
        "incomeQualityTTM": pd.to_numeric(base["operating_cashflow"], errors="coerce") / pd.to_numeric(base["net_income"], errors="coerce").replace(0, np.nan),
        "freeCashFlowOperatingCashFlowRatioTTM": base["free_cashflow"] / pd.to_numeric(base["operating_cashflow"], errors="coerce").replace(0, np.nan),
        "freeCashFlowYieldTTM": base["free_cashflow"] / base["marketCap"].replace(0, np.nan),
        "netDebtToEBITDATTM": (base["debt"] - pd.to_numeric(base["cash"], errors="coerce").fillna(0)) / base["ebitda"].replace(0, np.nan),
        "currentRatioTTM": pd.to_numeric(base["current_assets"], errors="coerce") / pd.to_numeric(base["current_liabilities"], errors="coerce").replace(0, np.nan),
        "interestCoverageRatioTTM": pd.to_numeric(base["operating_income"], errors="coerce") / pd.to_numeric(base["interest_expense"], errors="coerce").replace(0, np.nan),
        "priceToEarningsRatioTTM": base["marketCap"] / pd.to_numeric(base["net_income"], errors="coerce").replace(0, np.nan),
        "enterpriseValueMultipleTTM": base["enterprise_value"] / base["ebitda"].replace(0, np.nan),
        "stockBasedCompensationToRevenueTTM": pd.to_numeric(base["stock_comp"], errors="coerce") / pd.to_numeric(base["revenue"], errors="coerce").replace(0, np.nan),
        "capexToRevenueTTM": base["capex"] / pd.to_numeric(base["revenue"], errors="coerce").replace(0, np.nan),
        "revenuePerShareTTM": pd.to_numeric(base["revenue"], errors="coerce") / pd.to_numeric(base["shares"], errors="coerce").replace(0, np.nan),
        "freeCashFlowPerShareTTM": base["free_cashflow"] / pd.to_numeric(base["shares"], errors="coerce").replace(0, np.nan),
        "revenueGrowth": [
            _cagr(n, o, growth_years) for n, o in zip(base["revenue"], base["revenue_old"])
        ],
        "epsGrowth": [
            _cagr(_safe_div(n, s), _safe_div(o, so), growth_years)
            for n, s, o, so in zip(base["net_income"], base["shares"], base["net_income_old"], base["shares_old"])
        ],
        "fcfGrowth": [
            _cagr(n, o, growth_years) for n, o in zip(base["free_cashflow"], base["free_cashflow_old"])
        ],
        "sharesGrowth": [
            _cagr(n, o, growth_years) for n, o in zip(base["shares"], base["shares_old"])
        ],
        "fcfMarginTTM": base["free_cashflow"] / pd.to_numeric(base["revenue"], errors="coerce").replace(0, np.nan),
        "fundamental_source": "SEC",
        "fundamental_updated_at": _now_iso(),
        "fundamental_period": str(current_year),
    })
    eps_growth = pd.to_numeric(result["epsGrowth"], errors="coerce")
    pe = pd.to_numeric(result["priceToEarningsRatioTTM"], errors="coerce")
    result["forwardPriceToEarningsGrowthRatioTTM"] = pe / (eps_growth * 100).where(eps_growth > 0)
    return result.replace([np.inf, -np.inf], np.nan)
