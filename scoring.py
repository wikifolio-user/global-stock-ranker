from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np
import pandas as pd

from settings import CATEGORY_HIGH_PRIORITY, CATEGORY_MIN_COMPLETENESS, CATEGORY_WATCHLIST


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


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").replace([np.inf, -np.inf], np.nan)


def _piecewise_higher(s: pd.Series, thresholds: Iterable[tuple[float, float]], max_points: float) -> pd.Series:
    """Map higher-is-better values to points using ascending thresholds.

    thresholds: [(value_threshold, fraction_of_max), ...]
    """
    x = _num(s)
    out = pd.Series(0.0, index=x.index)
    for threshold, fraction in sorted(thresholds, key=lambda z: z[0]):
        out = out.where(~(x >= threshold), max_points * fraction)
    return out.where(x.notna(), 0.0)


def _piecewise_lower(s: pd.Series, thresholds: Iterable[tuple[float, float]], max_points: float) -> pd.Series:
    """Map lower-is-better values to points.

    thresholds should be ordered from strict/best to loose/worst values, each with a fraction.
    """
    x = _num(s)
    out = pd.Series(0.0, index=x.index)
    for threshold, fraction in sorted(thresholds, key=lambda z: z[0], reverse=True):
        out = out.where(~(x <= threshold), max_points * fraction)
    return out.where(x.notna(), 0.0)


def _safe_col(df: pd.DataFrame, name: str, default=np.nan) -> pd.Series:
    if name in df.columns:
        return _num(df[name])
    return pd.Series(default, index=df.index, dtype=float)


def add_scores(df: pd.DataFrame) -> pd.DataFrame:
    """Calculate a transparent 0-100 quantitative score.

    The model deliberately uses measurable proxies for qualitative categories such as moat
    and capital allocation. The UI labels those proxies so they are not mistaken for a full
    qualitative business analysis.
    """
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

    fcf_margin = (fcf_per_share / rev_per_share).replace([np.inf, -np.inf], np.nan)
    out["fcfMarginTTM"] = fcf_margin

    # 1) Unternehmensqualität: 20
    quality = (
        _piecewise_higher(roic, [(0.05, 0.2), (0.10, 0.5), (0.15, 0.75), (0.25, 1.0)], 8)
        + _piecewise_higher(op_margin, [(0.05, 0.2), (0.10, 0.5), (0.20, 0.8), (0.30, 1.0)], 4)
        + _piecewise_higher(gross_margin, [(0.20, 0.25), (0.35, 0.5), (0.50, 0.8), (0.65, 1.0)], 4)
        + _piecewise_higher(income_quality, [(0.6, 0.25), (0.8, 0.5), (1.0, 0.8), (1.2, 1.0)], 4)
    )

    # 2) Wachstum: 15
    growth_thresholds = [(-0.05, 0.1), (0.0, 0.25), (0.05, 0.5), (0.10, 0.75), (0.20, 1.0)]
    growth = (
        _piecewise_higher(rev_growth, growth_thresholds, 5)
        + _piecewise_higher(eps_growth, growth_thresholds, 5)
        + _piecewise_higher(fcf_growth, growth_thresholds, 5)
    )

    # 3) Free Cashflow: 15
    cashflow = (
        _piecewise_higher(fcf_margin, [(0.03, 0.25), (0.08, 0.5), (0.15, 0.8), (0.25, 1.0)], 6)
        + _piecewise_higher(fcf_ocf, [(0.5, 0.25), (0.7, 0.5), (0.85, 0.8), (0.95, 1.0)], 5)
        + _piecewise_higher(fcf_yield, [(0.01, 0.15), (0.025, 0.4), (0.05, 0.75), (0.08, 1.0)], 4)
    )

    # 4) Bilanz: 10. Negative net debt is treated as excellent.
    balance = (
        _piecewise_lower(net_debt_ebitda, [(0.0, 1.0), (1.0, 0.85), (2.0, 0.6), (3.0, 0.3)], 6)
        + _piecewise_higher(current_ratio, [(0.8, 0.2), (1.0, 0.5), (1.5, 0.8), (2.0, 1.0)], 2)
        + _piecewise_higher(interest_cover, [(1.5, 0.2), (3.0, 0.5), (6.0, 0.8), (10.0, 1.0)], 2)
    )

    # 5) Kapitalallokation: 10. Quantitative proxies only.
    dilution = -shares_growth  # shrinking share count => positive score
    capital_allocation = (
        _piecewise_higher(dilution, [(-0.05, 0.0), (-0.02, 0.25), (0.0, 0.6), (0.02, 0.85), (0.05, 1.0)], 5)
        + _piecewise_lower(sbc_rev, [(0.01, 1.0), (0.03, 0.8), (0.06, 0.5), (0.10, 0.2)], 3)
        + _piecewise_lower(capex_rev, [(0.03, 1.0), (0.07, 0.8), (0.15, 0.5), (0.25, 0.2)], 2)
    )

    # 6) Moat proxy: 10. High returns and margins are used as evidence, not proof, of moat.
    moat = (
        _piecewise_higher(roic, [(0.08, 0.2), (0.12, 0.5), (0.18, 0.8), (0.30, 1.0)], 4)
        + _piecewise_higher(gross_margin, [(0.20, 0.2), (0.35, 0.5), (0.50, 0.8), (0.65, 1.0)], 3)
        + _piecewise_higher(op_margin, [(0.05, 0.2), (0.10, 0.5), (0.20, 0.8), (0.30, 1.0)], 3)
    )

    # 7) Bewertung: 15. Negative/zero P/E and EV/EBITDA get no points.
    pe_clean = pe.where(pe > 0)
    ev_clean = ev_ebitda.where(ev_ebitda > 0)
    peg_clean = peg.where(peg > 0)
    valuation = (
        _piecewise_higher(fcf_yield, [(0.015, 0.15), (0.03, 0.4), (0.05, 0.75), (0.08, 1.0)], 6)
        + _piecewise_lower(pe_clean, [(12, 1.0), (18, 0.8), (25, 0.55), (35, 0.25)], 4)
        + _piecewise_lower(ev_clean, [(8, 1.0), (12, 0.8), (18, 0.5), (25, 0.2)], 3)
        + _piecewise_lower(peg_clean, [(0.8, 1.0), (1.2, 0.8), (1.8, 0.5), (2.5, 0.2)], 2)
    )

    # 8) Risiko: 5. Reward resilience; hard red flags below subtract from total.
    risk = (
        _piecewise_lower(net_debt_ebitda, [(0, 1.0), (1, 0.8), (2, 0.55), (3, 0.25)], 2)
        + _piecewise_higher(op_margin, [(0.0, 0.15), (0.05, 0.4), (0.12, 0.7), (0.20, 1.0)], 1.5)
        + _piecewise_higher(fcf_margin, [(0.0, 0.15), (0.05, 0.4), (0.10, 0.7), (0.20, 1.0)], 1.5)
    )

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

    # Red flag penalties: keep explicit and explainable.
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

    score_inputs = [
        roic, gross_margin, op_margin, income_quality, rev_growth, eps_growth, fcf_growth,
        fcf_margin, fcf_ocf, fcf_yield, net_debt_ebitda, current_ratio, interest_cover,
        shares_growth, sbc_rev, capex_rev, pe, ev_ebitda, peg,
    ]
    non_null = sum(s.notna().astype(int) for s in score_inputs)
    out["data_completeness"] = (non_null / len(score_inputs) * 100).round(0)

    return out


def category_from_score(score: float, completeness: float) -> str:
    if completeness < CATEGORY_MIN_COMPLETENESS:
        return "Daten prüfen"
    if score >= CATEGORY_HIGH_PRIORITY:
        return "Hohe Analysepriorität"
    if score >= CATEGORY_WATCHLIST:
        return "Beobachtungsliste"
    return "Nicht weiterverfolgen"
