"""Zentrale Einstellungen für den Global Stock Ranker.

Diese Datei ist absichtlich einfach gehalten. Für die meisten persönlichen Anpassungen
reicht es, nur diese Werte zu verändern und die Datei in GitHub zu speichern.
"""

APP_VERSION = "2.1"

# Ranking / Ansicht
TOP_N = 100
HISTORY_TOP_N = 300
HISTORY_KEEP_DAYS = 365
DEFAULT_MIN_MARKET_CAP_BN = 1.0
DEFAULT_MIN_COMPLETENESS = 60
CATEGORY_MIN_COMPLETENESS = 55
CATEGORY_HIGH_PRIORITY = 75
CATEGORY_WATCHLIST = 60

# Einstieg-Setup: Summe muss 1.00 ergeben.
ENTRY_WEIGHTS = {
    "quality": 0.25,
    "growth": 0.12,
    "cashflow": 0.20,
    "balance": 0.08,
    "valuation": 0.27,
    "risk_resilience": 0.08,
}

# These-Risiko: Schwellen und Strafpunkte.
THESIS_RISK = {
    "fcf_growth_below": 0.00,
    "fcf_growth_points": 12,
    "eps_growth_below": 0.00,
    "eps_growth_points": 10,
    "roic_below": 0.10,
    "roic_points": 12,
    "net_debt_ebitda_above": 3.0,
    "net_debt_points": 15,
    "shares_growth_above": 0.05,
    "shares_growth_points": 10,
    "negative_fcf_margin_points": 20,
    "score_delta_below": -5.0,
    "score_delta_points": 18,
    "red_flag_multiplier": 4,
}

# Research-Fokus. Das sind Analysehinweise, keine Kauf-/Verkaufsempfehlungen.
FOCUS = {
    "thesis_review_risk": 60,
    "entry_total_score": 75,
    "entry_setup_score": 72,
    "entry_valuation_score": 10,
    "good_quality_total": 72,
    "valuation_wait_below": 8,
    "observe_total": 60,
}

# Technischer Trend wird nur als Bestätigung verwendet.
TECHNICAL = {
    "up_price_vs_ma50_tolerance": 0.97,
    "down_price_vs_ma50_tolerance": 1.03,
}
