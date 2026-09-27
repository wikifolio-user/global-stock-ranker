from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from settings import SOURCE_QUALITY


FUNDAMENTAL_METRICS = [
    "returnOnInvestedCapitalTTM",
    "grossProfitMarginTTM",
    "operatingProfitMarginTTM",
    "incomeQualityTTM",
    "freeCashFlowOperatingCashFlowRatioTTM",
    "freeCashFlowYieldTTM",
    "netDebtToEBITDATTM",
    "currentRatioTTM",
    "interestCoverageRatioTTM",
    "priceToEarningsRatioTTM",
    "forwardPriceToEarningsGrowthRatioTTM",
    "enterpriseValueMultipleTTM",
    "stockBasedCompensationToRevenueTTM",
    "capexToRevenueTTM",
    "revenuePerShareTTM",
    "freeCashFlowPerShareTTM",
    "revenueGrowth",
    "epsGrowth",
    "fcfGrowth",
    "sharesGrowth",
    "fcfMarginTTM",
]

AUX_FUNDAMENTALS = [
    "marketCap",
    "forwardPERatio",
    "returnOnEquityTTM",
    "returnOnAssetsTTM",
    "valuation_zscore",
    "pe_5y_mean",
    "pe_5y_median",
    "beta",
    "targetMeanPrice",
    "targetHighPrice",
    "targetLowPrice",
    "analystCount",
    "recommendationMean",
]

PRICE_METRICS = [
    "price",
    "priceAvg50",
    "priceAvg200",
    "yearHigh",
    "yearLow",
    "changePercentage",
    "return1m",
    "return3m",
    "return6m",
    "return12m",
    "volume",
    "avgVolume20",
]


def load_cache(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except Exception:
        return pd.DataFrame()


def save_cache(df: pd.DataFrame, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, compression="gzip")


def upsert_cache(existing: pd.DataFrame, new: pd.DataFrame, key: str = "symbol") -> pd.DataFrame:
    if new is None or new.empty:
        return existing.copy() if existing is not None else pd.DataFrame()
    if existing is None or existing.empty:
        return new.drop_duplicates(key, keep="last").copy()
    old = existing.drop_duplicates(key, keep="last").set_index(key)
    fresh = new.drop_duplicates(key, keep="last").set_index(key)
    # Fresh non-null fields win; null fresh fields do not erase useful cached data.
    combined = fresh.combine_first(old)
    missing_old = old.index.difference(fresh.index)
    if len(missing_old):
        combined = pd.concat([combined, old.loc[missing_old]], axis=0)
    combined = combined[~combined.index.duplicated(keep="first")]
    return combined.reset_index()


def import_cache_bytes(uploaded_file) -> pd.DataFrame:
    if uploaded_file is None:
        return pd.DataFrame()
    name = str(getattr(uploaded_file, "name", "")).lower()
    compression = "gzip" if name.endswith(".gz") else None
    return pd.read_csv(uploaded_file, compression=compression)


def _numeric_non_null_count(df: pd.DataFrame, columns: Iterable[str]) -> pd.Series:
    cols = [c for c in columns if c in df.columns]
    if not cols:
        return pd.Series(0, index=df.index, dtype=int)
    frame = df[cols].apply(pd.to_numeric, errors="coerce")
    return frame.notna().sum(axis=1)


def select_next_symbols(
    universe: pd.DataFrame,
    provider_cache: pd.DataFrame,
    limit: int,
    non_us_first: bool = True,
    retry_after_days: int = 7,
    refresh_after_days: int = 14,
) -> list[str]:
    """Select missing or stale fundamentals without repeatedly hammering failures.

    Useful provider rows are kept until ``refresh_after_days`` old. Recent failures
    receive a cooldown, while a changed provider-symbol mapping bypasses the cooldown
    immediately. This lets the queue progress automatically and still refresh old data.
    """
    work = universe.copy()
    if "ishares_weight_pct" in work.columns:
        work["_weight"] = pd.to_numeric(work["ishares_weight_pct"], errors="coerce").fillna(0)
    else:
        work["_weight"] = 0.0

    skip: set[str] = set()
    if provider_cache is not None and not provider_cache.empty and "symbol" in provider_cache.columns:
        pc = provider_cache.copy()
        pc["_coverage"] = _numeric_non_null_count(pc, FUNDAMENTAL_METRICS)
        success_at = pd.to_datetime(
            pc.get("fundamental_updated_at", pd.Series(index=pc.index, dtype=object)),
            errors="coerce", utc=True,
        )
        attempt_at = pd.to_datetime(
            pc.get("fundamental_attempted_at", pc.get("fundamental_updated_at", pd.Series(index=pc.index, dtype=object))),
            errors="coerce", utc=True,
        )
        now = pd.Timestamp.now(tz="UTC")
        fresh_cutoff = now - pd.Timedelta(days=max(0, int(refresh_after_days)))
        useful_fresh = (pc["_coverage"] >= 5) & (success_at >= fresh_cutoff)
        skip.update(pc.loc[useful_fresh, "symbol"].astype(str))

        recent_cutoff = now - pd.Timedelta(days=max(0, int(retry_after_days)))
        recent_attempt = attempt_at >= recent_cutoff
        errors = pc.get("provider_error", pd.Series("", index=pc.index)).fillna("").astype(str)
        failed_recently = recent_attempt & errors.ne("")

        same_mapping = pd.Series(True, index=pc.index)
        if "provider_symbol" in pc.columns and "provider_symbol" in universe.columns:
            current_map = (
                universe[["symbol", "provider_symbol"]]
                .drop_duplicates("symbol", keep="last")
                .set_index("symbol")["provider_symbol"]
                .astype(str)
            )
            current_provider = pc["symbol"].astype(str).map(current_map).fillna("")
            cached_provider = pc["provider_symbol"].fillna("").astype(str)
            same_mapping = cached_provider.eq(current_provider)
        skip.update(pc.loc[failed_recently & same_mapping, "symbol"].astype(str))

    work = work[~work["symbol"].astype(str).isin(skip)].copy()
    if non_us_first and "country" in work.columns:
        work["_region_priority"] = np.where(work["country"].astype(str).eq("United States"), 1, 0)
        work = work.sort_values(["_region_priority", "_weight"], ascending=[True, False])
    else:
        work = work.sort_values("_weight", ascending=False)
    return work["symbol"].astype(str).head(int(limit)).tolist()


def select_stale_symbols(
    universe: pd.DataFrame,
    cache: pd.DataFrame,
    timestamp_col: str,
    max_age_hours: float,
    limit: int,
    failure_cooldown_hours: float = 6,
    non_us_first: bool = False,
) -> list[str]:
    """Return missing/stale symbols, prioritizing oldest data and skipping recent failures."""
    work = universe.copy()
    work["_weight"] = pd.to_numeric(work["ishares_weight_pct"], errors="coerce").fillna(0) if "ishares_weight_pct" in work.columns else pd.Series(0.0, index=work.index)
    now = pd.Timestamp.now(tz="UTC")

    if cache is None or cache.empty or "symbol" not in cache.columns:
        work["_ts"] = pd.NaT
        work["_failed_recently"] = False
    else:
        c = cache.drop_duplicates("symbol", keep="last").copy()
        ts = pd.to_datetime(c.get(timestamp_col, pd.Series(index=c.index, dtype=object)), errors="coerce", utc=True)
        attempt_col = "price_attempted_at" if timestamp_col.startswith("price") else "fundamental_attempted_at"
        attempt = pd.to_datetime(c.get(attempt_col, c.get(timestamp_col, pd.Series(index=c.index, dtype=object))), errors="coerce", utc=True)
        err = c.get("provider_error", pd.Series("", index=c.index)).fillna("").astype(str)
        c["_ts"] = ts
        c["_failed_recently"] = err.ne("") & (attempt >= now - pd.Timedelta(hours=float(failure_cooldown_hours)))
        work = work.merge(c[["symbol", "_ts", "_failed_recently"]], on="symbol", how="left")
        work["_failed_recently"] = work["_failed_recently"].eq(True)

    cutoff = now - pd.Timedelta(hours=float(max_age_hours))
    due = work["_ts"].isna() | (work["_ts"] < cutoff)
    work = work[due & ~work["_failed_recently"]].copy()
    work["_age_sort"] = work["_ts"].fillna(pd.Timestamp("1970-01-01", tz="UTC"))
    if non_us_first and "country" in work.columns:
        work["_region_priority"] = np.where(work["country"].astype(str).eq("United States"), 1, 0)
        work = work.sort_values(["_region_priority", "_age_sort", "_weight"], ascending=[True, True, False])
    else:
        work = work.sort_values(["_age_sort", "_weight"], ascending=[True, False])
    return work["symbol"].astype(str).head(int(limit)).tolist()


def _provider_value(provider: pd.DataFrame, metric: str) -> pd.Series:
    if provider is None or provider.empty or metric not in provider.columns:
        return pd.Series(dtype=float)
    return pd.to_numeric(provider.set_index("symbol")[metric], errors="coerce")


def _ensure_index(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty or "symbol" not in df.columns:
        return pd.DataFrame()
    return df.drop_duplicates("symbol", keep="last").set_index("symbol")


def assemble_snapshot(
    universe: pd.DataFrame,
    sec_cache: pd.DataFrame | None = None,
    finnhub_cache: pd.DataFrame | None = None,
    yahoo_cache: pd.DataFrame | None = None,
    price_cache: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Merge free sources with explicit field-level provenance.

    Precedence for current fundamentals is Finnhub -> SEC -> Yahoo. SEC receives the
    highest confidence weight, but Finnhub's Basic Financials are often TTM/current,
    so they are preferred when present. Yahoo is a fallback.
    """
    if universe is None or universe.empty:
        return pd.DataFrame()
    out = universe.drop_duplicates("symbol", keep="first").set_index("symbol").copy()
    providers = {
        "FINNHUB": _ensure_index(finnhub_cache if finnhub_cache is not None else pd.DataFrame()),
        "SEC": _ensure_index(sec_cache if sec_cache is not None else pd.DataFrame()),
        "YAHOO": _ensure_index(yahoo_cache if yahoo_cache is not None else pd.DataFrame()),
    }

    for metric in FUNDAMENTAL_METRICS + AUX_FUNDAMENTALS:
        values = pd.Series(np.nan, index=out.index, dtype=float)
        sources = pd.Series("", index=out.index, dtype=object)
        for source in ["FINNHUB", "SEC", "YAHOO"]:
            p = providers[source]
            if p.empty or metric not in p.columns:
                continue
            candidate = pd.to_numeric(p[metric], errors="coerce").reindex(out.index)
            take = values.isna() & candidate.notna()
            values.loc[take] = candidate.loc[take]
            sources.loc[take] = source
        out[metric] = values
        if metric in FUNDAMENTAL_METRICS:
            out[f"source__{metric}"] = sources

    # Fill metadata from Yahoo where the official universe has gaps.
    yahoo = providers["YAHOO"]
    if not yahoo.empty:
        for col in ["name", "sector", "country", "currency"]:
            if col in yahoo.columns:
                candidate = yahoo[col].reindex(out.index)
                if col not in out.columns:
                    out[col] = candidate
                else:
                    base = out[col].replace("", np.nan)
                    out[col] = base.combine_first(candidate)

    # Market prices / momentum.
    prices = _ensure_index(price_cache if price_cache is not None else pd.DataFrame())
    for metric in PRICE_METRICS:
        if not prices.empty and metric in prices.columns:
            out[metric] = pd.to_numeric(prices[metric], errors="coerce").reindex(out.index)
        else:
            out[metric] = np.nan
    # iShares price is USD-normalized and is a useful fallback for current price only.
    if "ishares_price_usd" in out.columns:
        fallback = pd.to_numeric(out["ishares_price_usd"], errors="coerce")
        missing = pd.to_numeric(out["price"], errors="coerce").isna()
        out.loc[missing, "price"] = fallback.loc[missing]
        out["price_source"] = np.where(missing & fallback.notna(), "ISHARES", "YAHOO")
        out["price_currency"] = np.where(missing & fallback.notna(), "USD", out.get("currency", ""))
    else:
        out["price_source"] = np.where(pd.to_numeric(out["price"], errors="coerce").notna(), "YAHOO", "")
        out["price_currency"] = out.get("currency", "")

    # Fundamental sources actually contributing to each row.
    source_cols = [f"source__{m}" for m in FUNDAMENTAL_METRICS if f"source__{m}" in out.columns]
    if source_cols:
        def source_list(row: pd.Series) -> str:
            used = sorted({str(x) for x in row if str(x) in {"SEC", "FINNHUB", "YAHOO"}})
            return "+".join(used)
        out["fundamental_sources"] = out[source_cols].apply(source_list, axis=1)
    else:
        out["fundamental_sources"] = ""

    # Raw input completeness before scoring.
    metric_frame = out[FUNDAMENTAL_METRICS].apply(pd.to_numeric, errors="coerce")
    out["raw_data_completeness"] = (metric_frame.notna().sum(axis=1) / len(FUNDAMENTAL_METRICS) * 100).round(0)

    # Confidence = 75% coverage + 20% source quality + 5% market/trend availability.
    def row_source_quality(row: pd.Series) -> float:
        weights = []
        for m in FUNDAMENTAL_METRICS:
            src = str(row.get(f"source__{m}", ""))
            if src:
                weights.append(SOURCE_QUALITY.get(src, 0.5))
        return float(np.mean(weights)) if weights else 0.0

    quality = out.apply(row_source_quality, axis=1)
    price_bonus = pd.to_numeric(out["price"], errors="coerce").notna().astype(float)
    out["data_confidence"] = (0.75 * out["raw_data_completeness"] + 20 * quality + 5 * price_bonus).clip(0, 100).round(0)
    out["confidence_label"] = pd.cut(
        out["data_confidence"],
        bins=[-1, 59.999, 79.999, 100.001],
        labels=["Niedrig", "Mittel", "Hoch"],
    ).astype(str)

    # Provider coverage flags for diagnostics.
    for source, provider in providers.items():
        covered = set(provider.index.astype(str)) if not provider.empty else set()
        out[f"has_{source.lower()}"] = out.index.astype(str).isin(covered)
    if not prices.empty:
        out["has_yahoo_price"] = out.index.astype(str).isin(set(prices.index.astype(str)))
    else:
        out["has_yahoo_price"] = False

    return out.reset_index()


def coverage_summary(
    snapshot: pd.DataFrame,
    min_completeness: float = 58,
    min_confidence: float = 62,
) -> dict[str, float | int | str]:
    """Coverage using the same eligibility rule as the visible ranking.

    Older versions used confidence alone for the KPI while the Top-100 also required
    completeness. That made the headline coverage disagree with what users could
    actually see. Version 3.1 aligns both definitions.
    """
    if snapshot is None or snapshot.empty:
        return {
            "universe": 0, "eligible": 0, "non_us_eligible": 0,
            "coverage_pct": 0.0, "non_us_coverage_pct": 0.0,
            "status": "Keine Daten",
        }
    conf = pd.to_numeric(
        snapshot.get("data_confidence", pd.Series(0, index=snapshot.index)), errors="coerce"
    ).fillna(0)
    completeness_col = "data_completeness" if "data_completeness" in snapshot.columns else "raw_data_completeness"
    completeness = pd.to_numeric(
        snapshot.get(completeness_col, pd.Series(0, index=snapshot.index)), errors="coerce"
    ).fillna(0)
    eligible_mask = (conf >= float(min_confidence)) & (completeness >= float(min_completeness))

    country = snapshot.get("country", pd.Series("", index=snapshot.index)).astype(str)
    n = len(snapshot)
    eligible = int(eligible_mask.sum())
    non_us = ~country.eq("United States")
    non_us_total = int(non_us.sum())
    non_us_eligible = int((eligible_mask & non_us).sum())
    ratio = eligible / n if n else 0
    non_us_ratio = non_us_eligible / non_us_total if non_us_total else 0
    if ratio >= 0.85 and non_us_ratio >= 0.75:
        status = "Global belastbar"
    elif ratio >= 0.55 and non_us_ratio >= 0.40:
        status = "Fortgeschritten / noch unvollständig"
    else:
        status = "Vorläufig"
    return {
        "universe": n,
        "eligible": eligible,
        "non_us_eligible": non_us_eligible,
        "coverage_pct": round(ratio * 100, 1),
        "non_us_coverage_pct": round(non_us_ratio * 100, 1),
        "status": status,
        "min_completeness": float(min_completeness),
        "min_confidence": float(min_confidence),
    }


def provider_diagnostics(frame: pd.DataFrame, provider: str) -> dict[str, object]:
    """Summarize provider cache quality for the diagnostics tab."""
    if frame is None or frame.empty:
        return {
            "Quelle": provider, "Cache-Zeilen": 0, "Nutzbar": 0,
            "Fehler": 0, "Rate-Limit": 0, "Letzte Aktualisierung": "—",
        }
    f = frame.copy()
    coverage = _numeric_non_null_count(f, FUNDAMENTAL_METRICS)
    errors = f.get("provider_error", pd.Series("", index=f.index)).fillna("").astype(str)
    updated = pd.to_datetime(f.get("fundamental_updated_at", pd.Series(index=f.index, dtype=object)), errors="coerce", utc=True)
    latest = updated.max()
    latest_text = latest.strftime("%Y-%m-%d %H:%M UTC") if pd.notna(latest) else "—"
    return {
        "Quelle": provider,
        "Cache-Zeilen": int(len(f)),
        "Nutzbar": int((coverage >= 5).sum()),
        "Fehler": int(errors.ne("").sum()),
        "Rate-Limit": int(errors.str.contains("RATE_LIMIT", case=False, regex=False).sum()),
        "Letzte Aktualisierung": latest_text,
    }


def provider_batch_stats(frame: pd.DataFrame) -> dict[str, int]:
    """Return compact counters for a freshly fetched provider batch."""
    if frame is None or frame.empty:
        return {"responses": 0, "useful": 0, "errors": 0, "rate_limits": 0}
    coverage = _numeric_non_null_count(frame, FUNDAMENTAL_METRICS)
    errors = frame.get("provider_error", pd.Series("", index=frame.index)).fillna("").astype(str)
    return {
        "responses": int(len(frame)),
        "useful": int((coverage >= 5).sum()),
        "errors": int(errors.ne("").sum()),
        "rate_limits": int(errors.str.contains("RATE_LIMIT", case=False, regex=False).sum()),
    }

def provenance_table(row: pd.Series) -> pd.DataFrame:
    labels = {
        "returnOnInvestedCapitalTTM": "ROIC",
        "grossProfitMarginTTM": "Bruttomarge",
        "operatingProfitMarginTTM": "Operative Marge",
        "freeCashFlowYieldTTM": "FCF Yield",
        "netDebtToEBITDATTM": "Net Debt / EBITDA",
        "priceToEarningsRatioTTM": "KGV",
        "enterpriseValueMultipleTTM": "EV / EBITDA",
        "revenueGrowth": "Umsatzwachstum",
        "epsGrowth": "EPS-Wachstum",
        "fcfGrowth": "FCF-Wachstum",
        "sharesGrowth": "Aktienzahl-Wachstum",
    }
    rows = []
    for metric, label in labels.items():
        rows.append({
            "Kennzahl": label,
            "Wert": row.get(metric, np.nan),
            "Quelle": row.get(f"source__{metric}", ""),
        })
    return pd.DataFrame(rows)


def cache_manifest(**frames: pd.DataFrame) -> str:
    payload = {}
    for name, frame in frames.items():
        payload[name] = {
            "rows": int(len(frame)) if frame is not None else 0,
            "columns": int(len(frame.columns)) if frame is not None and not frame.empty else 0,
        }
    return json.dumps(payload, ensure_ascii=False, indent=2)
