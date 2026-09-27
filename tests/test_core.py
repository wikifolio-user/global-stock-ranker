import numpy as np
import pandas as pd

from pipeline import assemble_snapshot
from scoring import add_scores
from signals import add_research_signals
from universe import parse_ishares_holdings_csv, to_provider_symbol


def test_universe_parser_and_symbol_mapping():
    text = '''iShares MSCI ACWI ETF\nFund Holdings as of,"Sep 24, 2026"\nTicker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,Quantity,Price,Location,Exchange,Currency,FX Rate,Market Currency,Accrual Date\n"AAPL","APPLE","Information Technology","Equity","1,000","4.0","1,000","10","100","United States","NASDAQ","USD","1","USD","-"\n"700","TENCENT","Communication","Equity","500","1.0","500","10","50","China","Hong Kong Exchanges And Clearing Ltd","USD","7.8","HKD","-"\n'''
    df = parse_ishares_holdings_csv(text)
    assert len(df) == 2
    assert df.loc[df.symbol == "AAPL", "provider_symbol"].iloc[0] == "AAPL"
    assert df.loc[df.symbol == "700", "provider_symbol"].iloc[0] == "0700.HK"
    assert to_provider_symbol("BRK B", "NASDAQ", "United States") == "BRK-B"


def test_provider_precedence_and_confidence():
    universe = pd.DataFrame({
        "symbol": ["AAA"], "provider_symbol": ["AAA"], "name": ["A"], "country": ["United States"],
        "sector": ["Technology"], "exchange": ["NASDAQ"], "currency": ["USD"], "ishares_price_usd": [100.0],
    })
    sec = pd.DataFrame({"symbol": ["AAA"], "returnOnInvestedCapitalTTM": [0.18], "priceToEarningsRatioTTM": [20.0]})
    finn = pd.DataFrame({"symbol": ["AAA"], "returnOnInvestedCapitalTTM": [0.22]})
    yahoo = pd.DataFrame({"symbol": ["AAA"], "grossProfitMarginTTM": [0.5]})
    snap = assemble_snapshot(universe, sec, finn, yahoo, pd.DataFrame())
    assert np.isclose(snap.loc[0, "returnOnInvestedCapitalTTM"], 0.22)
    assert snap.loc[0, "source__returnOnInvestedCapitalTTM"] == "FINNHUB"
    assert snap.loc[0, "source__priceToEarningsRatioTTM"] == "SEC"
    assert snap.loc[0, "source__grossProfitMarginTTM"] == "YAHOO"
    assert snap.loc[0, "price_source"] == "ISHARES"


def test_scoring_and_signals_bounds():
    row = {
        "symbol": "AAA", "data_confidence": 90,
        "returnOnInvestedCapitalTTM": .22, "grossProfitMarginTTM": .55, "operatingProfitMarginTTM": .24,
        "incomeQualityTTM": 1.1, "freeCashFlowOperatingCashFlowRatioTTM": .9, "freeCashFlowYieldTTM": .06,
        "netDebtToEBITDATTM": 1.0, "currentRatioTTM": 1.5, "interestCoverageRatioTTM": 8,
        "priceToEarningsRatioTTM": 18, "forwardPriceToEarningsGrowthRatioTTM": 1.2, "enterpriseValueMultipleTTM": 11,
        "stockBasedCompensationToRevenueTTM": .02, "capexToRevenueTTM": .05,
        "revenuePerShareTTM": 20, "freeCashFlowPerShareTTM": 3.5,
        "revenueGrowth": .10, "epsGrowth": .12, "fcfGrowth": .14, "sharesGrowth": -.01,
        "score_delta": 2.0, "fundamental_trend": "▲ Stärker",
        "price": 110, "priceAvg50": 105, "priceAvg200": 95, "yearHigh": 120, "yearLow": 70, "return6m": .12,
    }
    scored = add_scores(pd.DataFrame([row]))
    assert 0 <= scored.loc[0, "score_total"] <= 100
    assert pd.notna(scored.loc[0, "implied_fcf_growth_10y"])
    signaled = add_research_signals(scored)
    assert 0 <= signaled.loc[0, "entry_setup_score"] <= 100
    assert 0 <= signaled.loc[0, "thesis_risk_score"] <= 100
    assert "trend" in signaled.columns
    assert "exit_watch" in signaled.columns


def test_international_symbol_mapping_regressions():
    # London trailing-dot bug from live logs.
    assert to_provider_symbol("BP.", "London Stock Exchange", "United Kingdom") == "BP.L"
    assert to_provider_symbol("RR.", "London Stock Exchange", "United Kingdom") == "RR.L"
    assert to_provider_symbol("BA.", "London Stock Exchange", "United Kingdom") == "BA.L"

    # Country fallbacks cover common iShares exchange-label variations.
    assert to_provider_symbol("CBA", "ASX - All Markets", "Australia") == "CBA.AX"
    assert to_provider_symbol("BNP", "Euronext - Paris", "France") == "BNP.PA"
    assert to_provider_symbol("NOVO-B", "Nasdaq Copenhagen", "Denmark") == "NOVO-B.CO"
    assert to_provider_symbol("VOLV-B", "Nasdaq Stockholm", "Sweden") == "VOLV-B.ST"
    assert to_provider_symbol("NDA-FI", "Nasdaq Helsinki", "Finland") == "NDA-FI.HE"
    assert to_provider_symbol("PETR4", "B3 - Brasil Bolsa Balcao", "Brazil") == "PETR4.SA"


def test_failed_provider_rows_get_cooldown():
    from pipeline import select_next_symbols

    universe = pd.DataFrame({
        "symbol": ["AAA", "BBB", "CCC"],
        "country": ["France", "France", "France"],
        "ishares_weight_pct": [3.0, 2.0, 1.0],
    })
    cache = pd.DataFrame({
        "symbol": ["AAA"],
        "provider_error": ["HTTP 404"],
        "fundamental_updated_at": [pd.Timestamp.now(tz="UTC").isoformat()],
    })
    selected = select_next_symbols(universe, cache, 2, retry_after_days=7)
    assert selected == ["BBB", "CCC"]


def test_coverage_matches_ranking_thresholds():
    from pipeline import coverage_summary

    frame = pd.DataFrame({
        "symbol": ["A", "B", "C"],
        "country": ["United States", "France", "France"],
        "data_confidence": [80, 80, 59],
        "data_completeness": [70, 40, 90],
    })
    summary = coverage_summary(frame, min_completeness=55, min_confidence=60)
    assert summary["eligible"] == 1
    assert summary["non_us_eligible"] == 0


def test_mapping_change_bypasses_failure_cooldown():
    from pipeline import select_next_symbols

    universe = pd.DataFrame({
        "symbol": ["BP.", "AAA"],
        "provider_symbol": ["BP.L", "AAA.PA"],
        "country": ["United Kingdom", "France"],
        "ishares_weight_pct": [2.0, 1.0],
    })
    cache = pd.DataFrame({
        "symbol": ["BP."],
        "provider_symbol": ["BP..L"],
        "provider_error": ["HTTP 404"],
        "fundamental_updated_at": [pd.Timestamp.now(tz="UTC").isoformat()],
    })
    selected = select_next_symbols(universe, cache, 1, retry_after_days=7)
    assert selected == ["BP."]


def test_yahoo_chart_parser_without_crumb(monkeypatch):
    import data_sources

    class FakeResponse:
        status_code = 200
        ok = True

        def json(self):
            n = 260
            return {
                "chart": {
                    "result": [{
                        "timestamp": list(range(n)),
                        "indicators": {
                            "quote": [{
                                "close": [100 + i * 0.1 for i in range(n)],
                                "high": [101 + i * 0.1 for i in range(n)],
                                "low": [99 + i * 0.1 for i in range(n)],
                                "volume": [1000 + i for i in range(n)],
                            }],
                            "adjclose": [{"adjclose": [100 + i * 0.1 for i in range(n)]}],
                        },
                    }],
                    "error": None,
                }
            }

    def fake_get(*args, **kwargs):
        assert "/v8/finance/chart/" in args[0]
        assert "crumb" not in kwargs.get("params", {})
        return FakeResponse()

    monkeypatch.setattr(data_sources.requests, "get", fake_get)
    row = data_sources._yahoo_chart_row("TEST", "TEST", period="1y")
    assert row["provider_error"] == ""
    assert row["price_source"] == "YAHOO_CHART"
    assert row["price"] > 0
    assert row["priceAvg200"] > 0
    assert row["return12m"] > 0


def test_stale_selector_skips_fresh_and_prioritizes_missing():
    from pipeline import select_stale_symbols

    now = pd.Timestamp.now(tz="UTC")
    universe = pd.DataFrame({
        "symbol": ["A", "B", "C"],
        "country": ["United States", "France", "Japan"],
        "ishares_weight_pct": [3.0, 2.0, 1.0],
    })
    cache = pd.DataFrame({
        "symbol": ["A", "B"],
        "price_updated_at": [now.isoformat(), (now - pd.Timedelta(hours=30)).isoformat()],
        "provider_error": ["", ""],
    })
    selected = select_stale_symbols(
        universe, cache, "price_updated_at", max_age_hours=22, limit=2,
        failure_cooldown_hours=6,
    )
    assert selected == ["C", "B"]


def test_recent_price_failure_gets_cooldown():
    from pipeline import select_stale_symbols

    now = pd.Timestamp.now(tz="UTC")
    universe = pd.DataFrame({
        "symbol": ["A", "B"],
        "ishares_weight_pct": [2.0, 1.0],
    })
    cache = pd.DataFrame({
        "symbol": ["A"],
        "price_attempted_at": [now.isoformat()],
        "provider_error": ["RATE_LIMIT"],
    })
    selected = select_stale_symbols(
        universe, cache, "price_updated_at", max_age_hours=22, limit=2,
        failure_cooldown_hours=6,
    )
    assert selected == ["B"]


def test_fresh_useful_finnhub_rows_refresh_only_when_old():
    from pipeline import select_next_symbols

    now = pd.Timestamp.now(tz="UTC")
    universe = pd.DataFrame({
        "symbol": ["A", "B"],
        "provider_symbol": ["A.PA", "B.PA"],
        "country": ["France", "France"],
        "ishares_weight_pct": [2.0, 1.0],
    })
    cache = pd.DataFrame({
        "symbol": ["A"],
        "provider_symbol": ["A.PA"],
        "fundamental_updated_at": [(now - pd.Timedelta(days=2)).isoformat()],
        "provider_error": [""],
        "returnOnInvestedCapitalTTM": [0.15],
        "grossProfitMarginTTM": [0.4],
        "operatingProfitMarginTTM": [0.2],
        "freeCashFlowYieldTTM": [0.05],
        "priceToEarningsRatioTTM": [18.0],
    })
    selected = select_next_symbols(universe, cache, 2, refresh_after_days=14)
    assert selected == ["B"]
