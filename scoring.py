from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np
import pandas as pd

from settings import (
    CATEGORY_HIGH_PRIORITY,
    CATEGORY_MIN_COMPLETENESS,
    CATEGORY_WATCHLIST,
    DCF_DISCOUNT_RATE,
    DCF_GROWTH_CAP,
    DCF_GROWTH_FLOOR,
    DCF_TERMINAL_GROWTH,
    DCF_YEARS,
    DEFAULT_MIN_COMPLETENESS,
    DEFAULT_MIN_CONFIDENCE,
    GLOBAL_LITE_MIN_COMPLETENESS,
    GLOBAL_LITE_MIN_CONFIDENCE,
    GLOBAL_LITE_MIN_GROUPS,
    GLOBAL_LITE_RANKING_PENALTY,
)


@dataclass(frozen=True)
class ScoreComponent:
    name: str
    max_points: float


COMPONENTS = [
    ScoreComponent("Unternehmensqualität", 20),
    ScoreComponent("Wachstum", 15),
    ScoreComponent("Free Cashflow", 15),
    ScoreComponent("Bilanz", 10),
    ScoreComponent("Management & Kapitalallokation", 10),
    ScoreComponent("Moat-Proxy", 10),
    ScoreComponent("Bewertung", 15),
    ScoreComponent("Risiko", 5),
]

SCORE_INPUTS = [
    "returnOnInvestedCapitalTTM",
    "grossProfitMarginTTM",
    "operatingProfitMarginTTM",
    "incomeQualityTTM",
    "revenueGrowth",
    "epsGrowth",
    "fcfGrowth",
    "fcfMarginTTM",
    "freeCashFlowOperatingCashFlowRatioTTM",
    "freeCashFlowYieldTTM",
    "netDebtToEBITDATTM",
    "currentRatioTTM",
    "interestCoverageRatioTTM",
    "sharesGrowth",
    "stockBasedCompensationToRevenueTTM",
    "capexToRevenueTTM",
    "priceToEarningsRatioTTM",
    "enterpriseValueMultipleTTM",
    "forwardPriceToEarningsGrowthRatioTTM",
]


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").replace([np.inf, -np.inf], np.nan)


def _piecewise_higher(s: pd.Series, thresholds: Iterable[tuple[float, float]], max_points: float) -> pd.Series:
    x = _num(s)
    out = pd.Series(0.0, index=x.index)
    for threshold, fraction in sorted(thresholds, key=lambda z: z[0]):
        out = out.where(~(x >= threshold), max_points * fraction)
    return out.where(x.notna(), 0.0)


def _piecewise_lower(s: pd.Series, thresholds: Iterable[tuple[float, float]], max_points: float) -> pd.Series:
    x = _num(s)
    out = pd.Series(0.0, index=x.index)
    for threshold, fraction in sorted(thresholds, key=lambda z: z[0], reverse=True):
        out = out.where(~(x <= threshold), max_points * fraction)
    return out.where(x.notna(), 0.0)


def _safe_col(df: pd.DataFrame, name: str, default=np.nan) -> pd.Series:
    if name in df.columns:
        return _num(df[name])
    return pd.Series(default, index=df.index, dtype=float)


def _normalized_component(parts: list[tuple[pd.Series, pd.Series, float]], max_points: float) -> pd.Series:
    """Scale a component to its full weight using only observed submetrics.

    Missing fields are not silently treated as bad fundamentals. The separate Data
    Confidence score applies the uncertainty haircut used for ranking. This is
    important in a multi-source global dataset where coverage differs by country.
    """
    if not parts:
        return pd.Series(dtype=float)
    idx = parts[0][0].index
    raw = pd.Series(0.0, index=idx)
    available_max = pd.Series(0.0, index=idx)
    for points, source_values, sub_max in parts:
        raw = raw + points.fillna(0.0)
        available_max = available_max + source_values.notna().astype(float) * float(sub_max)
    scaled = raw / available_max.replace(0, np.nan) * float(max_points)
    return scaled.fillna(0.0).clip(0, max_points)


def _dcf_value_ratio(fcf_yield: float, growth: float, discount_rate: float, terminal_growth: float, years: int) -> float:
    """DCF fair-value/current-price ratio using current FCF yield as starting cash flow.

    Price is normalized to 1.0; FCF0 therefore equals the current FCF yield. This makes
    the proxy comparable across securities without needing absolute currency values.
    """
    if not np.isfinite(fcf_yield) or fcf_yield <= 0:
        return np.nan
    if not np.isfinite(growth) or discount_rate <= terminal_growth or years <= 0:
        return np.nan
    g = float(np.clip(growth, DCF_GROWTH_FLOOR, DCF_GROWTH_CAP))
    pv = 0.0
    fcf = float(fcf_yield)
    for year in range(1, years + 1):
        fcf *= 1.0 + g
        pv += fcf / ((1.0 + discount_rate) ** year)
    terminal_fcf = fcf * (1.0 + terminal_growth)
    terminal_value = terminal_fcf / (discount_rate - terminal_growth)
    pv += terminal_value / ((1.0 + discount_rate) ** years)
    return pv


def _implied_growth(fcf_yield: float) -> float:
    """Solve the 10-year FCF growth implied by price = 1 with a bisection search."""
    if not np.isfinite(fcf_yield) or fcf_yield <= 0:
        return np.nan
    lo, hi = -0.20, 0.60
    vlo = _dcf_value_ratio(fcf_yield, lo, DCF_DISCOUNT_RATE, DCF_TERMINAL_GROWTH, DCF_YEARS)
    vhi = _dcf_value_ratio(fcf_yield, hi, DCF_DISCOUNT_RATE, DCF_TERMINAL_GROWTH, DCF_YEARS)
    if not np.isfinite(vlo) or not np.isfinite(vhi) or not (vlo <= 1.0 <= vhi):
        return np.nan
    for _ in range(70):
        mid = (lo + hi) / 2.0
        vmid = _dcf_value_ratio(fcf_yield, mid, DCF_DISCOUNT_RATE, DCF_TERMINAL_GROWTH, DCF_YEARS)
        if vmid < 1.0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def add_dcf_proxies(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    fcf_yield = _safe_col(out, "freeCashFlowYieldTTM")
    rev_g = _safe_col(out, "revenueGrowth")
    eps_g = _safe_col(out, "epsGrowth")
    fcf_g = _safe_col(out, "fcfGrowth")

    expected_growth = pd.concat([rev_g, eps_g, fcf_g], axis=1).median(axis=1, skipna=True)
    expected_growth = expected_growth.clip(DCF_GROWTH_FLOOR, DCF_GROWTH_CAP)
    out["dcf_assumed_growth"] = expected_growth
    out["dcf_fair_value_ratio"] = [
        _dcf_value_ratio(y, g, DCF_DISCOUNT_RATE, DCF_TERMINAL_GROWTH, DCF_YEARS)
        if pd.notna(y) and pd.notna(g) else np.nan
        for y, g in zip(fcf_yield, expected_growth)
    ]
    out["dcf_margin_of_safety"] = pd.to_numeric(out["dcf_fair_value_ratio"], errors="coerce") - 1.0
    out["implied_fcf_growth_10y"] = [_implied_growth(y) if pd.notna(y) else np.nan for y in fcf_yield]
    return out


def add_scores(df: pd.DataFrame) -> pd.DataFrame:
    """Transparent 0-100 quantitative score with conservative missing-data handling."""
    out = df.copy()

    roic = _safe_col(out, "returnOnInvestedCapitalTTM")
    gross_margin = _safe_col(out, "grossProfitMarginTTM")
    op_margin = _safe_col(out, "operatingProfitMarginTTM")
    income_quality = _safe_col(out, "incomeQualityTTM")
    fcf_ocf = _safe_col(out, "freeCashFlowOperatingCashFlowRatioTTM")
    fcf_yield = _safe_col(out, "freeCashFlowYieldTTM")
    net_debt_ebitda = _safe_col(out, "netDebtToEBITDATTM")
    current_ratio = _safe_col(out, "currentRatioTTM")
    interest_cover = _safe_col(out, "interestCoverageRatioTTM")
    pe = _safe_col(out, "priceToEarningsRatioTTM")
    peg = _safe_col(out, "forwardPriceToEarningsGrowthRatioTTM")
    ev_ebitda = _safe_col(out, "enterpriseValueMultipleTTM")
    sbc_rev = _safe_col(out, "stockBasedCompensationToRevenueTTM")
    capex_rev = _safe_col(out, "capexToRevenueTTM")
    rev_per_share = _safe_col(out, "revenuePerShareTTM")
    fcf_per_share = _safe_col(out, "freeCashFlowPerShareTTM")

    rev_growth = _safe_col(out, "revenueGrowth")
    eps_growth = _safe_col(out, "epsGrowth")
    fcf_growth = _safe_col(out, "fcfGrowth")
    shares_growth = _safe_col(out, "sharesGrowth")

    direct_fcf_margin = _safe_col(out, "fcfMarginTTM")
    derived_fcf_margin = (fcf_per_share / rev_per_share).replace([np.inf, -np.inf], np.nan)
    fcf_margin = direct_fcf_margin.combine_first(derived_fcf_margin)
    out["fcfMarginTTM"] = fcf_margin

    q_roic = _piecewise_higher(roic, [(0.05, 0.2), (0.10, 0.5), (0.15, 0.75), (0.25, 1.0)], 8)
    q_op = _piecewise_higher(op_margin, [(0.05, 0.2), (0.10, 0.5), (0.20, 0.8), (0.30, 1.0)], 4)
    q_gross = _piecewise_higher(gross_margin, [(0.20, 0.25), (0.35, 0.5), (0.50, 0.8), (0.65, 1.0)], 4)
    q_income = _piecewise_higher(income_quality, [(0.6, 0.25), (0.8, 0.5), (1.0, 0.8), (1.2, 1.0)], 4)
    quality = _normalized_component([(q_roic, roic, 8), (q_op, op_margin, 4), (q_gross, gross_margin, 4), (q_income, income_quality, 4)], 20)

    growth_thresholds = [(-0.05, 0.1), (0.0, 0.25), (0.05, 0.5), (0.10, 0.75), (0.20, 1.0)]
    g_rev = _piecewise_higher(rev_growth, growth_thresholds, 5)
    g_eps = _piecewise_higher(eps_growth, growth_thresholds, 5)
    g_fcf = _piecewise_higher(fcf_growth, growth_thresholds, 5)
    growth = _normalized_component([(g_rev, rev_growth, 5), (g_eps, eps_growth, 5), (g_fcf, fcf_growth, 5)], 15)

    cf_margin_p = _piecewise_higher(fcf_margin, [(0.03, 0.25), (0.08, 0.5), (0.15, 0.8), (0.25, 1.0)], 6)
    cf_conv_p = _piecewise_higher(fcf_ocf, [(0.5, 0.25), (0.7, 0.5), (0.85, 0.8), (0.95, 1.0)], 5)
    cf_yield_p = _piecewise_higher(fcf_yield, [(0.01, 0.15), (0.025, 0.4), (0.05, 0.75), (0.08, 1.0)], 4)
    cashflow = _normalized_component([(cf_margin_p, fcf_margin, 6), (cf_conv_p, fcf_ocf, 5), (cf_yield_p, fcf_yield, 4)], 15)

    b_debt = _piecewise_lower(net_debt_ebitda, [(0.0, 1.0), (1.0, 0.85), (2.0, 0.6), (3.0, 0.3)], 6)
    b_current = _piecewise_higher(current_ratio, [(0.8, 0.2), (1.0, 0.5), (1.5, 0.8), (2.0, 1.0)], 2)
    b_interest = _piecewise_higher(interest_cover, [(1.5, 0.2), (3.0, 0.5), (6.0, 0.8), (10.0, 1.0)], 2)
    balance = _normalized_component([(b_debt, net_debt_ebitda, 6), (b_current, current_ratio, 2), (b_interest, interest_cover, 2)], 10)

    dilution = -shares_growth
    ca_dilution = _piecewise_higher(dilution, [(-0.05, 0.0), (-0.02, 0.25), (0.0, 0.6), (0.02, 0.85), (0.05, 1.0)], 5)
    ca_sbc = _piecewise_lower(sbc_rev, [(0.01, 1.0), (0.03, 0.8), (0.06, 0.5), (0.10, 0.2)], 3)
    ca_capex = _piecewise_lower(capex_rev, [(0.03, 1.0), (0.07, 0.8), (0.15, 0.5), (0.25, 0.2)], 2)
    capital_allocation = _normalized_component([(ca_dilution, shares_growth, 5), (ca_sbc, sbc_rev, 3), (ca_capex, capex_rev, 2)], 10)

    m_roic = _piecewise_higher(roic, [(0.08, 0.2), (0.12, 0.5), (0.18, 0.8), (0.30, 1.0)], 4)
    m_gross = _piecewise_higher(gross_margin, [(0.20, 0.2), (0.35, 0.5), (0.50, 0.8), (0.65, 1.0)], 3)
    m_op = _piecewise_higher(op_margin, [(0.05, 0.2), (0.10, 0.5), (0.20, 0.8), (0.30, 1.0)], 3)
    moat = _normalized_component([(m_roic, roic, 4), (m_gross, gross_margin, 3), (m_op, op_margin, 3)], 10)

    pe_clean = pe.where(pe > 0)
    ev_clean = ev_ebitda.where(ev_ebitda > 0)
    peg_clean = peg.where(peg > 0)
    v_fcf = _piecewise_higher(fcf_yield, [(0.015, 0.15), (0.03, 0.4), (0.05, 0.75), (0.08, 1.0)], 6)
    v_pe = _piecewise_lower(pe_clean, [(12, 1.0), (18, 0.8), (25, 0.55), (35, 0.25)], 4)
    v_ev = _piecewise_lower(ev_clean, [(8, 1.0), (12, 0.8), (18, 0.5), (25, 0.2)], 3)
    v_peg = _piecewise_lower(peg_clean, [(0.8, 1.0), (1.2, 0.8), (1.8, 0.5), (2.5, 0.2)], 2)
    valuation = _normalized_component([(v_fcf, fcf_yield, 6), (v_pe, pe_clean, 4), (v_ev, ev_clean, 3), (v_peg, peg_clean, 2)], 15)

    r_debt = _piecewise_lower(net_debt_ebitda, [(0, 1.0), (1, 0.8), (2, 0.55), (3, 0.25)], 2)
    r_op = _piecewise_higher(op_margin, [(0.0, 0.15), (0.05, 0.4), (0.12, 0.7), (0.20, 1.0)], 1.5)
    r_fcf = _piecewise_higher(fcf_margin, [(0.0, 0.15), (0.05, 0.4), (0.10, 0.7), (0.20, 1.0)], 1.5)
    risk = _normalized_component([(r_debt, net_debt_ebitda, 2), (r_op, op_margin, 1.5), (r_fcf, fcf_margin, 1.5)], 5)

    out["score_quality"] = quality.clip(0, 20)
    out["score_growth"] = growth.clip(0, 15)
    out["score_cashflow"] = cashflow.clip(0, 15)
    out["score_balance"] = balance.clip(0, 10)
    out["score_capital_allocation"] = capital_allocation.clip(0, 10)
    out["score_moat"] = moat.clip(0, 10)
    out["score_valuation"] = valuation.clip(0, 15)
    out["score_risk"] = risk.clip(0, 5)

    base_total = out[[
        "score_quality", "score_growth", "score_cashflow", "score_balance",
        "score_capital_allocation", "score_moat", "score_valuation", "score_risk"
    ]].sum(axis=1)

    penalty = pd.Series(0.0, index=out.index)
    penalty += np.where(fcf_margin < 0, 8.0, 0.0)
    penalty += np.where(roic < 0, 5.0, 0.0)
    penalty += np.where(net_debt_ebitda > 4, 5.0, 0.0)
    penalty += np.where(op_margin < 0, 4.0, 0.0)
    penalty += np.where(shares_growth > 0.08, 4.0, 0.0)
    penalty += np.where(sbc_rev > 0.15, 3.0, 0.0)
    penalty += np.where((pe > 60) & (eps_growth < 0.15), 3.0, 0.0)

    out["red_flag_penalty"] = penalty
    out["score_total"] = (base_total - penalty).clip(0, 100)

    score_frame = pd.concat([_safe_col(out, c) for c in SCORE_INPUTS], axis=1)
    out["data_completeness"] = (score_frame.notna().sum(axis=1) / len(SCORE_INPUTS) * 100).round(0)

    # Two evidence tiers: Full uses the complete model, while Global Lite admits
    # non-US companies with a smaller but broad-enough set of global metrics. Lite
    # rows receive both an uncertainty haircut and an explicit ranking penalty.
    confidence = _safe_col(out, "data_confidence")
    lite_conf = _safe_col(out, "global_lite_confidence")
    lite_comp = _safe_col(out, "global_lite_completeness")
    lite_groups = _safe_col(out, "global_lite_groups")
    country = out.get("country", pd.Series("", index=out.index)).astype(str)

    full_tier = (out["data_completeness"] >= DEFAULT_MIN_COMPLETENESS) & (confidence >= DEFAULT_MIN_CONFIDENCE)
    lite_tier = (
        ~country.eq("United States")
        & ~full_tier
        & (lite_comp >= GLOBAL_LITE_MIN_COMPLETENESS)
        & (lite_conf >= GLOBAL_LITE_MIN_CONFIDENCE)
        & (lite_groups >= GLOBAL_LITE_MIN_GROUPS)
    )
    out["data_tier"] = np.select(
        [full_tier, lite_tier], ["Full", "Global Lite"], default="Unvollständig"
    )
    out["effective_data_confidence"] = np.where(lite_tier, lite_conf, confidence)
    out["effective_data_completeness"] = np.where(lite_tier, lite_comp, out["data_completeness"])

    effective_conf = pd.to_numeric(out["effective_data_confidence"], errors="coerce").fillna(0).clip(0, 100)
    confidence_factor = 0.45 + 0.55 * (effective_conf / 100.0)
    tier_factor = pd.Series(0.72, index=out.index, dtype=float)
    tier_factor.loc[full_tier] = 1.0
    tier_factor.loc[lite_tier] = float(GLOBAL_LITE_RANKING_PENALTY)
    out["score_confidence_adjusted"] = (out["score_total"] * confidence_factor).clip(0, 100)
    out["ranking_score"] = (out["score_confidence_adjusted"] * tier_factor).clip(0, 100)

    out = add_dcf_proxies(out)
    return out


def category_from_score(score: float, completeness: float, confidence: float | None = None) -> str:
    if completeness < CATEGORY_MIN_COMPLETENESS or (confidence is not None and pd.notna(confidence) and confidence < 55):
        return "Daten prüfen"
    if score >= CATEGORY_HIGH_PRIORITY:
        return "Hohe Analysepriorität"
    if score >= CATEGORY_WATCHLIST:
        return "Beobachtungsliste"
    return "Nicht weiterverfolgen"
