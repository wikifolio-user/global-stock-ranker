from __future__ import annotations

import numpy as np
import pandas as pd


def make_demo_universe(n: int = 450, seed: int = 42) -> pd.DataFrame:
    """Synthetic dataset so UI/scoring can be evaluated without live providers."""
    rng = np.random.default_rng(seed)
    symbols = [f"DEMO{i:04d}" for i in range(1, n + 1)]
    countries = rng.choice(["United States", "Germany", "Japan", "Switzerland", "United Kingdom", "France", "Canada", "Australia"], n)
    sectors = rng.choice(["Information Technology", "Industrials", "Health Care", "Consumer Discretionary", "Communication", "Materials"], n)
    market_cap = np.exp(rng.normal(np.log(12e9), 1.25, n)).clip(2e8, 2e12)
    roic = rng.normal(0.16, 0.10, n).clip(-0.2, 0.55)
    gross = rng.normal(0.48, 0.16, n).clip(0.08, 0.9)
    op = (gross * rng.uniform(0.18, 0.62, n) - rng.normal(0.01, 0.04, n)).clip(-0.2, 0.5)
    fcf_margin = (op * rng.uniform(0.65, 1.15, n)).clip(-0.25, 0.45)
    rev_growth = rng.normal(0.10, 0.11, n).clip(-0.35, 0.55)
    eps_growth = (rev_growth + rng.normal(0.03, 0.11, n)).clip(-0.6, 0.8)
    fcf_growth = (eps_growth + rng.normal(0.01, 0.13, n)).clip(-0.7, 1.0)
    shares_growth = rng.normal(0.005, 0.035, n).clip(-0.12, 0.18)
    fcf_yield = rng.normal(0.045, 0.027, n).clip(-0.04, 0.16)
    pe = (1 / np.maximum(0.012, fcf_yield + rng.normal(0.0, 0.015, n))).clip(5, 90)
    peg = (pe / np.maximum(5, (eps_growth.clip(0.01) * 100))).clip(0.2, 5)
    ev_ebitda = (pe * rng.uniform(0.35, 0.75, n)).clip(3, 45)
    revenue_ps = rng.uniform(5, 180, n)
    fcf_ps = revenue_ps * fcf_margin
    price = np.exp(rng.normal(np.log(80), 0.8, n)).clip(3, 800)
    ma200 = price * rng.normal(0.96, 0.12, n).clip(0.65, 1.35)
    ma50 = ma200 * rng.normal(1.03, 0.08, n).clip(0.75, 1.3)
    year_high = np.maximum.reduce([price, ma50, ma200]) * rng.uniform(1.02, 1.35, n)
    year_low = np.minimum.reduce([price, ma50, ma200]) * rng.uniform(0.55, 0.95, n)

    df = pd.DataFrame({
        "symbol": symbols,
        "provider_symbol": symbols,
        "name": [f"Synthetic Company {i}" for i in range(1, n + 1)],
        "country": countries,
        "sector": sectors,
        "exchange": "DEMO",
        "currency": "USD",
        "ishares_weight_pct": rng.uniform(0.01, 0.8, n),
        "marketCap": market_cap,
        "returnOnInvestedCapitalTTM": roic,
        "grossProfitMarginTTM": gross,
        "operatingProfitMarginTTM": op,
        "incomeQualityTTM": rng.normal(1.0, 0.25, n).clip(0.1, 2),
        "freeCashFlowOperatingCashFlowRatioTTM": rng.normal(0.85, 0.14, n).clip(0.05, 1.1),
        "freeCashFlowYieldTTM": fcf_yield,
        "netDebtToEBITDATTM": rng.normal(1.2, 1.3, n).clip(-4, 7),
        "currentRatioTTM": rng.normal(1.6, 0.7, n).clip(0.2, 5),
        "interestCoverageRatioTTM": np.exp(rng.normal(np.log(7), 0.9, n)).clip(0, 60),
        "priceToEarningsRatioTTM": pe,
        "forwardPriceToEarningsGrowthRatioTTM": peg,
        "enterpriseValueMultipleTTM": ev_ebitda,
        "stockBasedCompensationToRevenueTTM": rng.beta(1.2, 14, n).clip(0, 0.25),
        "capexToRevenueTTM": rng.beta(1.5, 9, n).clip(0, 0.4),
        "revenuePerShareTTM": revenue_ps,
        "freeCashFlowPerShareTTM": fcf_ps,
        "fcfMarginTTM": fcf_margin,
        "revenueGrowth": rev_growth,
        "epsGrowth": eps_growth,
        "fcfGrowth": fcf_growth,
        "sharesGrowth": shares_growth,
        "price": price,
        "priceAvg50": ma50,
        "priceAvg200": ma200,
        "yearHigh": year_high,
        "yearLow": year_low,
        "return1m": rng.normal(0.01, 0.08, n),
        "return3m": rng.normal(0.03, 0.14, n),
        "return6m": rng.normal(0.06, 0.20, n),
        "return12m": rng.normal(0.12, 0.30, n),
        "changePercentage": rng.normal(0.0, 2.2, n).clip(-10, 10),
        "data_confidence": rng.uniform(72, 99, n),
        "fundamental_sources": "DEMO",
        "confidence_label": "Hoch",
    })
    for c in [
        "returnOnInvestedCapitalTTM", "grossProfitMarginTTM", "operatingProfitMarginTTM",
        "incomeQualityTTM", "freeCashFlowOperatingCashFlowRatioTTM", "freeCashFlowYieldTTM",
        "netDebtToEBITDATTM", "currentRatioTTM", "interestCoverageRatioTTM",
        "priceToEarningsRatioTTM", "forwardPriceToEarningsGrowthRatioTTM",
        "enterpriseValueMultipleTTM", "stockBasedCompensationToRevenueTTM", "capexToRevenueTTM",
        "revenuePerShareTTM", "freeCashFlowPerShareTTM", "revenueGrowth", "epsGrowth",
        "fcfGrowth", "sharesGrowth", "fcfMarginTTM",
    ]:
        df[f"source__{c}"] = "DEMO"
    return df
