# Global Stock Ranker 3.4

Mobile-friendly Streamlit research app for a global equity universe using free data sources: iShares MSCI ACWI holdings, SEC EDGAR, Finnhub Basic Financials and Yahoo Chart prices.

Version 3.4 focuses on three problems from live use:

- **No repeated full reloads:** the smart cache skips fresh values and advances through missing/stale queues.
- **Better global coverage:** international stocks can enter a clearly labelled **Global Lite** tier when broad but incomplete free-data evidence is available. Lite rows receive an explicit ranking penalty and never masquerade as Full data.
- **Durable automation:** the GitHub Actions agent continues the queues every six hours and commits compressed cache files to `data_cache/` so Streamlit can restore them after redeploys.

Start with `UPGRADE_3_4.md`. For iPhone-only setup, see `INSTALL_AGENT_IPHONE.md`.
