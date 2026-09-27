from __future__ import annotations

import numpy as np
import pandas as pd

from settings import ENTRY_WEIGHTS, FOCUS, TECHNICAL, THESIS_RISK


def _num(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series(np.nan, index=df.index, dtype=float)
    return pd.to_numeric(df[col], errors="coerce")


def _technical_trend(df: pd.DataFrame) -> pd.Series:
    price = _num(df, "price")
    ma50 = _num(df, "priceAvg50")
    ma200 = _num(df, "priceAvg200")
    out = pd.Series("—", index=df.index, dtype="object")
    has = price.notna() & ma50.notna() & ma200.notna()
    up = has & (price > ma200) & (ma50 > ma200) & (price >= ma50 * TECHNICAL["up_price_vs_ma50_tolerance"])
    down = has & (price < ma200) & (ma50 < ma200) & (price <= ma50 * TECHNICAL["down_price_vs_ma50_tolerance"])
    out.loc[has] = "→ Neutral"
    out.loc[up] = "▲ Aufwärts"
    out.loc[down] = "▼ Abwärts"
    return out


def add_research_signals(df: pd.DataFrame) -> pd.DataFrame:
    """Add transparent research signals, not personalized trade instructions."""
    out = df.copy()
    if out.empty:
        return out

    quality = _num(out, "score_quality") / 20
    growth = _num(out, "score_growth") / 15
    cashflow = _num(out, "score_cashflow") / 15
    balance = _num(out, "score_balance") / 10
    valuation = _num(out, "score_valuation") / 15
    risk_resilience = _num(out, "score_risk") / 5

    # Entry setup is deliberately different from the main score: valuation and cash generation matter more.
    entry = 100 * (
        ENTRY_WEIGHTS["quality"] * quality.fillna(0)
        + ENTRY_WEIGHTS["growth"] * growth.fillna(0)
        + ENTRY_WEIGHTS["cashflow"] * cashflow.fillna(0)
        + ENTRY_WEIGHTS["balance"] * balance.fillna(0)
        + ENTRY_WEIGHTS["valuation"] * valuation.fillna(0)
        + ENTRY_WEIGHTS["risk_resilience"] * risk_resilience.fillna(0)
    )
    out["entry_setup_score"] = entry.clip(0, 100).round(1)

    fcf_growth = _num(out, "fcfGrowth")
    eps_growth = _num(out, "epsGrowth")
    roic = _num(out, "returnOnInvestedCapitalTTM")
    net_debt = _num(out, "netDebtToEBITDATTM")
    shares_growth = _num(out, "sharesGrowth")
    fcf_margin = _num(out, "fcfMarginTTM")
    score_delta = _num(out, "score_delta")
    penalty = _num(out, "red_flag_penalty").fillna(0)

    thesis_risk = penalty * THESIS_RISK["red_flag_multiplier"]
    thesis_risk += np.where(fcf_growth < THESIS_RISK["fcf_growth_below"], THESIS_RISK["fcf_growth_points"], 0)
    thesis_risk += np.where(eps_growth < THESIS_RISK["eps_growth_below"], THESIS_RISK["eps_growth_points"], 0)
    thesis_risk += np.where(roic < THESIS_RISK["roic_below"], THESIS_RISK["roic_points"], 0)
    thesis_risk += np.where(net_debt > THESIS_RISK["net_debt_ebitda_above"], THESIS_RISK["net_debt_points"], 0)
    thesis_risk += np.where(shares_growth > THESIS_RISK["shares_growth_above"], THESIS_RISK["shares_growth_points"], 0)
    thesis_risk += np.where(fcf_margin < 0, THESIS_RISK["negative_fcf_margin_points"], 0)
    thesis_risk += np.where(score_delta <= THESIS_RISK["score_delta_below"], THESIS_RISK["score_delta_points"], 0)
    out["thesis_risk_score"] = pd.Series(thesis_risk, index=out.index).clip(0, 100).round(0)

    flags = []
    for idx in out.index:
        items: list[str] = []
        if pd.notna(fcf_growth.loc[idx]) and fcf_growth.loc[idx] < THESIS_RISK["fcf_growth_below"]:
            items.append("FCF schrumpft")
        if pd.notna(eps_growth.loc[idx]) and eps_growth.loc[idx] < THESIS_RISK["eps_growth_below"]:
            items.append("EPS schrumpft")
        if pd.notna(roic.loc[idx]) and roic.loc[idx] < THESIS_RISK["roic_below"]:
            items.append("ROIC < 10 %")
        if pd.notna(net_debt.loc[idx]) and net_debt.loc[idx] > THESIS_RISK["net_debt_ebitda_above"]:
            items.append("hohe Verschuldung")
        if pd.notna(shares_growth.loc[idx]) and shares_growth.loc[idx] > THESIS_RISK["shares_growth_above"]:
            items.append("Verwässerung > 5 %")
        if pd.notna(fcf_margin.loc[idx]) and fcf_margin.loc[idx] < 0:
            items.append("negativer FCF")
        if pd.notna(score_delta.loc[idx]) and score_delta.loc[idx] <= THESIS_RISK["score_delta_below"]:
            items.append("Score stark gefallen")
        flags.append(", ".join(items) if items else "keine harten Warnsignale")
    out["thesis_flags"] = flags

    out["technical_trend"] = _technical_trend(out)

    def combined(row: pd.Series) -> str:
        fund = str(row.get("fundamental_trend", "● Neu"))
        tech = str(row.get("technical_trend", "—"))
        if fund.startswith("▼"):
            return "▼ Fundamental schwächer"
        if fund.startswith("▲") and tech.startswith("▲"):
            return "▲ Bestätigt"
        if fund.startswith("▲"):
            return "↗ Fundamental stärker"
        if tech.startswith("▼"):
            return "↘ Kurs schwach"
        if tech.startswith("▲"):
            return "↗ Kurs stark"
        if fund.startswith("●"):
            return "● Neu"
        return "→ Stabil"

    out["trend"] = out.apply(combined, axis=1)

    def valuation_band(v: float) -> str:
        if pd.isna(v):
            return "Daten fehlen"
        if v >= 11.5:
            return "Attraktiv"
        if v >= 8.0:
            return "Fair"
        return "Anspruchsvoll"

    out["valuation_band"] = _num(out, "score_valuation").map(valuation_band)

    def focus(row: pd.Series) -> str:
        total = row.get("score_total", np.nan)
        entry_s = row.get("entry_setup_score", np.nan)
        val = row.get("score_valuation", np.nan)
        trisk = row.get("thesis_risk_score", np.nan)
        trend = str(row.get("trend", ""))
        if pd.notna(trisk) and trisk >= FOCUS["thesis_review_risk"]:
            return "These prüfen"
        if pd.notna(total) and total >= FOCUS["entry_total_score"] and pd.notna(entry_s) and entry_s >= FOCUS["entry_setup_score"] and pd.notna(val) and val >= FOCUS["entry_valuation_score"] and not (trend.startswith("▼") or trend.startswith("↘")):
            return "Einstieg analysieren"
        if pd.notna(total) and total >= FOCUS["good_quality_total"] and pd.notna(val) and val < FOCUS["valuation_wait_below"]:
            return "Qualität gut / Bewertung warten"
        if pd.notna(total) and total >= FOCUS["observe_total"]:
            return "Beobachten"
        return "Kein starkes Setup"

    out["research_focus"] = out.apply(focus, axis=1)

    price = _num(out, "price")
    year_high = _num(out, "yearHigh")
    year_low = _num(out, "yearLow")
    out["distance_52w_high"] = (price / year_high - 1).where((price > 0) & (year_high > 0))
    out["distance_52w_low"] = (price / year_low - 1).where((price > 0) & (year_low > 0))

    return out
