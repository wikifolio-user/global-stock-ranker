# Global Stock Ranker 3.3

Mobile-friendly Streamlit research app for a global equity universe using free data sources: iShares MSCI ACWI holdings, SEC EDGAR, Finnhub Basic Financials and Yahoo Chart prices.

Version 3.3 adds an intelligent stale-aware cache, one-click refresh orchestration, progress feedback and an optional GitHub Actions background data agent. Fresh data is reused instead of downloaded again. The scheduled agent persists compressed cache files under `data_cache/`, allowing Streamlit to restore data after redeploys.

See `UPGRADE_3_3.md` for the iPhone-friendly upgrade and GitHub Actions setup.
