# Global Stock Ranker 3.1

A mobile-friendly Streamlit research app for a global equity universe using free data sources.

Core sources: iShares MSCI ACWI holdings, SEC EDGAR, Finnhub Basic Financials and Yahoo/yfinance. The app combines fundamental quality, growth, free cash flow, balance sheet, valuation, trend, DCF proxies, source provenance and Data Confidence.

Version 3.1 focuses on international ticker reliability and data-pipeline progress. It fixes malformed Yahoo-style symbols such as `BP..L`, adds robust exchange/country suffix inference, prevents failed symbols from consuming every enrichment batch, aligns coverage with ranking eligibility, and expands provider diagnostics.

For iPhone-only deployment and upgrade steps, read `README_IPHONE_OHNE_TERMINAL.md` and `UPGRADE_3_1.md`.
