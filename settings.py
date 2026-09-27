"""Zentrale Konfiguration des Global Stock Ranker 3.1.

Die Datei ist bewusst einfach gehalten. Die meisten persönlichen Anpassungen können
hier vorgenommen werden, ohne die Daten- oder UI-Logik anzufassen.
"""

APP_VERSION = "3.2"

# -------------------------
# Universum / Ranking
# -------------------------
TOP_N = 100
HISTORY_TOP_N = 350
HISTORY_KEEP_DAYS = 730
DEFAULT_MIN_COMPLETENESS = 55
DEFAULT_MIN_CONFIDENCE = 60
EXCLUDE_SPECIAL_SECTORS_DEFAULT = True
SPECIAL_SECTORS = {"Financials", "Real Estate"}

# Offizielle iShares-ACWI-Positionen (Large/Mid Caps, Industrie- + Schwellenländer).
ACWI_HOLDINGS_URL = (
    "https://www.ishares.com/us/products/239600/"
    "ishares-msci-acwi-etf/latest-holdings.csv"
)
UNIVERSE_CACHE_HOURS = 24

# -------------------------
# Kostenlose Datenquellen
# -------------------------
# Finnhub: kostenloser API-Key empfohlen. Kleine Batches respektieren Free-Limits besser.
FINNHUB_BASE_URL = "https://finnhub.io/api/v1"
FINNHUB_DEFAULT_BATCH = 100
FINNHUB_MAX_BATCH_UI = 500
FINNHUB_DELAY_SECONDS = 1.05
FINNHUB_RETRY_AFTER_SECONDS = 61

# Yahoo Fundamentals sind in 3.2 im UI deaktiviert: quoteSummary ist auf Cloud-IP-Adressen
# häufig crumb-/auth-gesperrt. Yahoo wird im Kern nur noch über den crumb-freien v8-Chart-
# Endpunkt für Kurs-/Trenddaten verwendet. yfinance bleibt optional für Alt-/Detailcode.
YAHOO_DEFAULT_BATCH = 80
YAHOO_MAX_BATCH_UI = 300
YAHOO_WORKERS = 1
YAHOO_PRICE_BATCH = 25
YAHOO_PRICE_WORKERS = 3
YAHOO_PRICE_PAUSE_SECONDS = 1.25
YAHOO_PRICE_PERIOD = "1y"

# SEC EDGAR: offizielle, kostenlose US-XBRL-Daten. SEC_USER_AGENT mit Kontakt setzen.
SEC_BASE_URL = "https://data.sec.gov"
SEC_WWW_BASE_URL = "https://www.sec.gov"
SEC_REQUEST_DELAY_SECONDS = 0.13  # konservativ unter 10 Requests/Sekunde
SEC_SCREEN_CURRENT_YEAR_OFFSET = 1  # aktuelles Jahr - 1
SEC_SCREEN_GROWTH_YEARS = 3
SEC_CACHE_DAYS = 7

# -------------------------
# Scoring 0-100
# -------------------------
CATEGORY_MIN_COMPLETENESS = 55
CATEGORY_HIGH_PRIORITY = 75
CATEGORY_WATCHLIST = 60

# Einstieg-Setup: Summe = 1.00. DCF-Overlay wird separat ergänzt.
ENTRY_WEIGHTS = {
    "quality": 0.23,
    "growth": 0.12,
    "cashflow": 0.20,
    "balance": 0.08,
    "valuation": 0.29,
    "risk_resilience": 0.08,
}

# DCF-Proxy. Bewusst konservativ und nur als zusätzlicher Bewertungsbaustein.
DCF_DISCOUNT_RATE = 0.10
DCF_TERMINAL_GROWTH = 0.025
DCF_YEARS = 10
DCF_GROWTH_FLOOR = -0.08
DCF_GROWTH_CAP = 0.22

# These-Risiko: Schwellen und Strafpunkte.
THESIS_RISK = {
    "fcf_growth_below": 0.00,
    "fcf_growth_points": 12,
    "eps_growth_below": 0.00,
    "eps_growth_points": 10,
    "revenue_growth_below": 0.00,
    "revenue_growth_points": 7,
    "roic_below": 0.10,
    "roic_points": 12,
    "net_debt_ebitda_above": 3.0,
    "net_debt_points": 15,
    "shares_growth_above": 0.05,
    "shares_growth_points": 10,
    "negative_fcf_margin_points": 20,
    "score_delta_below": -5.0,
    "score_delta_points": 18,
    "dcf_mos_below": -0.30,
    "dcf_mos_points": 8,
    "red_flag_multiplier": 4,
}

# Research-Fokus. Analysehinweise, keine automatische Orderlogik.
FOCUS = {
    "thesis_review_risk": 60,
    "urgent_thesis_review_risk": 78,
    "entry_total_score": 75,
    "entry_setup_score": 72,
    "entry_valuation_score": 10,
    "entry_confidence": 70,
    "good_quality_total": 72,
    "valuation_wait_below": 8,
    "observe_total": 60,
}

TECHNICAL = {
    "up_price_vs_ma50_tolerance": 0.97,
    "down_price_vs_ma50_tolerance": 1.03,
    "momentum_confirm_6m": 0.05,
}

# Datenquellen-Gewichte für den Data-Confidence-Score.
SOURCE_QUALITY = {
    "SEC": 1.00,
    "FINNHUB": 0.92,
    "YAHOO": 0.75,
    "ISHARES": 0.85,
    "DERIVED": 0.90,
    "DEMO": 0.30,
}
