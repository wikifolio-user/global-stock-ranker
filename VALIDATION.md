# Validation – Version 3.4.0

Offline validation covers:

- Python syntax compilation of app, pipeline, refresh engine, data sources, scoring and background agent.
- Source precedence and Data Confidence.
- International ticker mapping regressions.
- Yahoo v8 chart parsing without crumb authentication.
- Price failure cooldown and UTC-safe stale selection.
- Finnhub failure cooldown and refresh windows.
- **Queue progression for thin successful Finnhub responses** (regression for repeated re-downloads).
- **Global Lite eligibility** only for non-US securities with broad evidence.
- **Explicit Global Lite ranking penalty** relative to Full data.
- Coverage summary consistency with Full + Global Lite ranking eligibility.
- GitHub Actions workflow included at `.github/workflows/stock-data-agent.yml` plus visible iPhone copy `STOCK_DATA_AGENT_WORKFLOW.yml`.

Live third-party endpoints are not exercised by the offline suite; runtime provider diagnostics remain visible in the app.
