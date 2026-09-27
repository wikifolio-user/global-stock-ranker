from __future__ import annotations

import numpy as np
import pandas as pd

from settings import ENTRY_WEIGHTS, FOCUS, TECHNICAL, THESIS_RISK


def _num(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series(np.nan, index=df.index, dtype=float)
    return pd.to_numeric(df[col], errors="coerce").replace([np.inf, -np.inf], np.nan)


def _scale_component(series: pd.Series, maximum: float) -> pd.Series:
    return (_num(pd.DataFrame({"x": series}), "x") / maximum * 100).clip(0, 100)


def add_research_signals(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if out.empty:
        return out

    quality = _scale_component(_num(out, "score_quality"), 20)
    growth = _scale_component(_num(out, "score_growth"), 15)
    cashflow = _scale_component(_num(out, "score_cashflow"), 15)
    balance = _scale_component(_num(out, "score_balance"), 10)
    valuation = _scale_component(_num(out, "score_valuation"), 15)
    resilience = _scale_component(_num(out, "score_risk"), 5)

    entry = (
        quality * ENTRY_WEIGHTS["quality"]
        + growth * ENTRY_WEIGHTS["growth"]
        + cashflow * ENTRY_WEIGHTS["cashflow"]
        + balance * ENTRY_WEIGHTS["balance"]
        + valuation * ENTRY_WEIGHTS["valuation"]
        + resilience * ENTRY_WEIGHTS["risk_resilience"]
    )

    # DCF proxy adds a small bounded overlay instead of dominating the score.
    mos = _num(out, "dcf_margin_of_safety")
    dcf_overlay = pd.Series(0.0, index=out.index)
    dcf_overlay += np.where(mos >= 0.30, 5.0, 0.0)
    dcf_overlay += np.where((mos >= 0.10) & (mos < 0.30), 2.5, 0.0)
    dcf_overlay += np.where((mos <= -0.25) & mos.notna(), -5.0, 0.0)
    dcf_overlay += np.where((mos < -0.10) & (mos > -0.25), -2.5, 0.0)
    out["entry_setup_score"] = (entry + dcf_overlay).clip(0, 100).round(0)

    # Thesis / exit-watch risk: deterioration matters more than pure price weakness.
    risk = pd.Series(0.0, index=out.index)
    flags: list[list[str]] = [[] for _ in range(len(out))]

    def add_flag(mask: pd.Series, points: float, label: str):
        nonlocal risk
        m = mask.fillna(False)
        risk = risk + np.where(m, points, 0.0)
        for i, yes in enumerate(m.tolist()):
            if yes:
                flags[i].append(label)

    fcf_growth = _num(out, "fcfGrowth")
    eps_growth = _num(out, "epsGrowth")
    revenue_growth = _num(out, "revenueGrowth")
    roic = _num(out, "returnOnInvestedCapitalTTM")
    net_debt = _num(out, "netDebtToEBITDATTM")
    shares_growth = _num(out, "sharesGrowth")
    fcf_margin = _num(out, "fcfMarginTTM")
    score_delta = _num(out, "score_delta")
    penalty = _num(out, "red_flag_penalty")

    add_flag(fcf_growth < THESIS_RISK["fcf_growth_below"], THESIS_RISK["fcf_growth_points"], "FCF schrumpft")
    add_flag(eps_growth < THESIS_RISK["eps_growth_below"], THESIS_RISK["eps_growth_points"], "EPS schrumpft")
    add_flag(revenue_growth < THESIS_RISK["revenue_growth_below"], THESIS_RISK["revenue_growth_points"], "Umsatz schrumpft")
    add_flag(roic < THESIS_RISK["roic_below"], THESIS_RISK["roic_points"], "ROIC unter Schwelle")
    add_flag(net_debt > THESIS_RISK["net_debt_ebitda_above"], THESIS_RISK["net_debt_points"], "Verschuldung hoch")
    add_flag(shares_growth > THESIS_RISK["shares_growth_above"], THESIS_RISK["shares_growth_points"], "Verwässerung erhöht")
    add_flag(fcf_margin < 0, THESIS_RISK["negative_fcf_margin_points"], "FCF-Marge negativ")
    add_flag(score_delta < THESIS_RISK["score_delta_below"], THESIS_RISK["score_delta_points"], "Score deutlich gefallen")
    add_flag(mos < THESIS_RISK["dcf_mos_below"], THESIS_RISK["dcf_mos_points"], "DCF-Proxy anspruchsvoll")
    risk += penalty.fillna(0) * THESIS_RISK["red_flag_multiplier"]

    out["thesis_risk_score"] = risk.clip(0, 100).round(0)
    out["thesis_flags"] = [", ".join(x) if x else "Keine starke quantitative Warnung" for x in flags]

    price = _num(out, "price")
    ma50 = _num(out, "priceAvg50")
    ma200 = _num(out, "priceAvg200")
    ret6m = _num(out, "return6m")

    def tech_label(i: int) -> str:
        p, m50, m200, r6 = price.iloc[i], ma50.iloc[i], ma200.iloc[i], ret6m.iloc[i]
        if pd.isna(p) or pd.isna(m200):
            return "● Kein Kurstrend"
        strong = p >= m200 and (pd.isna(m50) or p >= m50 * TECHNICAL["up_price_vs_ma50_tolerance"])
        weak = p < m200 and (pd.isna(m50) or p < m50 * TECHNICAL["down_price_vs_ma50_tolerance"])
        if strong and (pd.isna(r6) or r6 >= TECHNICAL["momentum_confirm_6m"]):
            return "▲ Aufwärtstrend"
        if weak:
            return "▼ Abwärtstrend"
        return "→ Neutral"

    out["technical_trend"] = [tech_label(i) for i in range(len(out))]

    def combined(row: pd.Series) -> str:
        fund = str(row.get("fundamental_trend", "→ Stabil"))
        tech = str(row.get("technical_trend", "● Kein Kurstrend"))
        risk_score = row.get("thesis_risk_score", np.nan)
        if pd.notna(risk_score) and risk_score >= FOCUS["thesis_review_risk"]:
            return "▼ Fundamental schwächer"
        if fund.startswith("▲") and tech.startswith("▲"):
            return "▲ Bestätigt"
        if fund.startswith("▲"):
            return "↗ Fundamental stärker"
        if fund.startswith("▼"):
            return "▼ Fundamental schwächer"
        if tech.startswith("▼"):
            return "↘ Kurs schwach"
        if tech.startswith("▲"):
            return "↗ Kurs stark"
        if fund.startswith("●"):
            return "● Neu"
        return "→ Stabil"

    out["trend"] = out.apply(combined, axis=1)

    def valuation_band(row: pd.Series) -> str:
        v = row.get("score_valuation", np.nan)
        z = row.get("valuation_zscore", np.nan)
        dcf = row.get("dcf_margin_of_safety", np.nan)
        if pd.isna(v):
            return "Daten fehlen"
        if v >= 11.5 and (pd.isna(dcf) or dcf >= -0.10) and (pd.isna(z) or z <= 0.5):
            return "Attraktiv"
        if v >= 8.0:
            return "Fair"
        return "Anspruchsvoll"

    out["valuation_band"] = out.apply(valuation_band, axis=1)

    def exit_watch(row: pd.Series) -> str:
        r = row.get("thesis_risk_score", np.nan)
        fund = str(row.get("fundamental_trend", ""))
        tech = str(row.get("technical_trend", ""))
        val = row.get("score_valuation", np.nan)
        mosv = row.get("dcf_margin_of_safety", np.nan)
        if pd.notna(r) and r >= FOCUS["urgent_thesis_review_risk"]:
            return "Dringend These prüfen"
        if pd.notna(r) and r >= FOCUS["thesis_review_risk"]:
            return "These prüfen"
        if fund.startswith("▼") and tech.startswith("▼"):
            return "Fundamental + Kurs prüfen"
        if (pd.notna(val) and val < 5) or (pd.notna(mosv) and mosv < -0.35):
            return "Bewertung prüfen"
        return "Keine starke Warnung"

    out["exit_watch"] = out.apply(exit_watch, axis=1)

    def focus(row: pd.Series) -> str:
        total = row.get("score_total", np.nan)
        entry_s = row.get("entry_setup_score", np.nan)
        val = row.get("score_valuation", np.nan)
        trisk = row.get("thesis_risk_score", np.nan)
        trend = str(row.get("trend", ""))
        confidence = row.get("data_confidence", np.nan)
        if pd.notna(trisk) and trisk >= FOCUS["thesis_review_risk"]:
            return "These prüfen"
        if pd.notna(confidence) and confidence < FOCUS["entry_confidence"]:
            return "Daten vervollständigen"
        if (
            pd.notna(total) and total >= FOCUS["entry_total_score"]
            and pd.notna(entry_s) and entry_s >= FOCUS["entry_setup_score"]
            and pd.notna(val) and val >= FOCUS["entry_valuation_score"]
            and not (trend.startswith("▼") or trend.startswith("↘"))
        ):
            return "Einstieg analysieren"
        if pd.notna(total) and total >= FOCUS["good_quality_total"] and pd.notna(val) and val < FOCUS["valuation_wait_below"]:
            return "Qualität gut / Bewertung warten"
        if pd.notna(total) and total >= FOCUS["observe_total"]:
            return "Beobachten"
        return "Kein starkes Setup"

    out["research_focus"] = out.apply(focus, axis=1)
    year_high = _num(out, "yearHigh")
    year_low = _num(out, "yearLow")
    out["distance_52w_high"] = (price / year_high - 1).where((price > 0) & (year_high > 0))
    out["distance_52w_low"] = (price / year_low - 1).where((price > 0) & (year_low > 0))
    return out
