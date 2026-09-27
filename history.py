from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd


HISTORY_METRICS = [
    "score_total",
    "ranking_score",
    "score_confidence_adjusted",
    "score_quality",
    "score_growth",
    "score_cashflow",
    "score_balance",
    "score_capital_allocation",
    "score_moat",
    "score_valuation",
    "score_risk",
    "red_flag_penalty",
    "data_completeness",
    "data_confidence",
    "returnOnInvestedCapitalTTM",
    "freeCashFlowYieldTTM",
    "revenueGrowth",
    "epsGrowth",
    "fcfGrowth",
    "sharesGrowth",
    "netDebtToEBITDATTM",
    "priceToEarningsRatioTTM",
    "fcfMarginTTM",
    "dcf_margin_of_safety",
    "implied_fcf_growth_10y",
]


def load_history(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(path)
    except Exception:
        return pd.DataFrame()
    if "snapshot_date" in df.columns:
        df["snapshot_date"] = pd.to_datetime(df["snapshot_date"], errors="coerce").dt.date.astype("string")
    return df


def make_snapshot(scored: pd.DataFrame, snapshot_date: date | None = None, top_n: int = 350) -> pd.DataFrame:
    if scored.empty:
        return pd.DataFrame()
    d = snapshot_date or datetime.now().date()
    rank_cols = [c for c in ["ranking_score", "score_total", "data_confidence"] if c in scored.columns]
    ranked = scored.sort_values(rank_cols, ascending=[False] * len(rank_cols)).copy()
    ranked["history_rank"] = np.arange(1, len(ranked) + 1)
    ranked = ranked.head(top_n)
    cols = ["symbol", "history_rank"]
    for c in ["name", "country", "sector", "exchange", "currency", "fundamental_sources"] + HISTORY_METRICS:
        if c in ranked.columns and c not in cols:
            cols.append(c)
    snap = ranked[cols].copy()
    snap.insert(0, "snapshot_date", d.isoformat())
    return snap


def append_snapshot(scored: pd.DataFrame, path: str | Path, snapshot_date: date | None = None, top_n: int = 350, keep_days: int = 730) -> pd.DataFrame:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    snap = make_snapshot(scored, snapshot_date=snapshot_date, top_n=top_n)
    if snap.empty:
        return load_history(path)
    history = load_history(path)
    d = str(snap["snapshot_date"].iloc[0])
    if not history.empty and "snapshot_date" in history.columns:
        history = history[history["snapshot_date"].astype(str) != d]
    merged = pd.concat([history, snap], ignore_index=True, sort=False)
    cutoff = datetime.now().date() - timedelta(days=keep_days)
    dates = pd.to_datetime(merged["snapshot_date"], errors="coerce")
    merged = merged[dates.dt.date >= cutoff].copy()
    merged = merged.sort_values(["snapshot_date", "history_rank"], ascending=[True, True])
    merged.to_csv(path, index=False, compression="gzip")
    return merged


def merge_imported_history(existing: pd.DataFrame, imported: pd.DataFrame) -> pd.DataFrame:
    if imported.empty:
        return existing
    required = {"snapshot_date", "symbol", "history_rank", "score_total"}
    if not required.issubset(imported.columns):
        raise ValueError("Historie benötigt snapshot_date, symbol, history_rank und score_total.")
    frames = [df for df in [existing, imported] if not df.empty]
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True, sort=False)
    out["snapshot_date"] = pd.to_datetime(out["snapshot_date"], errors="coerce").dt.date.astype("string")
    out = out.dropna(subset=["snapshot_date", "symbol"])
    out = out.drop_duplicates(["snapshot_date", "symbol"], keep="last")
    return out.sort_values(["snapshot_date", "history_rank"], ascending=[True, True])


def previous_snapshot(history: pd.DataFrame, current_date: date | None = None) -> pd.DataFrame:
    if history.empty or "snapshot_date" not in history.columns:
        return pd.DataFrame()
    today = current_date or datetime.now().date()
    dates = pd.to_datetime(history["snapshot_date"], errors="coerce").dt.date
    eligible = history.loc[dates < today].copy()
    if eligible.empty:
        return pd.DataFrame()
    last_date = pd.to_datetime(eligible["snapshot_date"], errors="coerce").max().date().isoformat()
    return eligible[eligible["snapshot_date"].astype(str) == last_date].copy()


def add_history_deltas(current: pd.DataFrame, history: pd.DataFrame, current_date: date | None = None) -> pd.DataFrame:
    out = current.copy()
    if out.empty:
        return out
    prev = previous_snapshot(history, current_date=current_date)
    if prev.empty:
        out["score_delta"] = np.nan
        out["history_rank_previous"] = np.nan
        out["rank_delta"] = np.nan
        out["fundamental_trend"] = "● Neu"
        return out

    keep = ["symbol", "history_rank", "score_total"]
    for c in ["score_quality", "score_growth", "score_cashflow", "score_valuation", "red_flag_penalty", "data_confidence", "dcf_margin_of_safety"]:
        if c in prev.columns:
            keep.append(c)
    prev = prev[keep].drop_duplicates("symbol").copy()
    prev = prev.rename(columns={c: f"prev_{c}" for c in prev.columns if c != "symbol"})
    out = out.merge(prev, on="symbol", how="left")

    out["score_delta"] = pd.to_numeric(out["score_total"], errors="coerce") - pd.to_numeric(out.get("prev_score_total"), errors="coerce")
    current_rank = pd.to_numeric(out.get("history_rank"), errors="coerce")
    prev_rank = pd.to_numeric(out.get("prev_history_rank"), errors="coerce")
    out["history_rank_previous"] = prev_rank
    out["rank_delta"] = prev_rank - current_rank

    def label(row: pd.Series) -> str:
        ds, dr = row.get("score_delta"), row.get("rank_delta")
        if pd.isna(ds):
            return "● Neu"
        if ds >= 3 or (pd.notna(dr) and dr >= 15 and ds >= 0):
            return "▲ Stärker"
        if ds <= -3 or (pd.notna(dr) and dr <= -15 and ds <= 0):
            return "▼ Schwächer"
        return "→ Stabil"

    out["fundamental_trend"] = out.apply(label, axis=1)
    return out


def symbol_history(history: pd.DataFrame, symbol: str) -> pd.DataFrame:
    if history.empty:
        return pd.DataFrame()
    out = history[history["symbol"].astype(str) == str(symbol)].copy()
    if out.empty:
        return out
    out["snapshot_date"] = pd.to_datetime(out["snapshot_date"], errors="coerce")
    return out.sort_values("snapshot_date")
