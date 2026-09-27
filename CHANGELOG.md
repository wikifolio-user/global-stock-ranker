# Changelog

## 3.3 – One-click Update & Background Data Agent

- New **Alles intelligent aktualisieren** button with automatic sequence and progress display.
- Stale-aware cache: current data is reused instead of downloaded again.
- Universe refresh window: 7 days.
- Price/trend refresh window: 22 hours; recent failures receive a 6-hour cooldown.
- SEC refresh window: 7 days.
- Finnhub good fundamentals refresh after 14 days; recent failures keep the existing cooldown.
- Separate success and attempt timestamps prevent failed requests from making stale data look fresh.
- Scheduled GitHub Actions agent added; it continues the data queue in the background and commits durable compressed cache files to `data_cache/`.
- Streamlit automatically seeds an empty local cache from `data_cache/` after redeploy/reboot.
- Manual update controls remain available only under an advanced expander.

## 3.2 – Yahoo Cloud Hardening

- Yahoo quoteSummary fundamentals disabled for live use because cloud IPs frequently receive 401/crumb failures.
- Core Yahoo price/trend collection switched to the crumb-free v8 chart endpoint.
- Conservative batching, throttling and rate-limit stop behavior added.

## 3.1 – Symbol Mapping & Pipeline Fixes

- International Yahoo/Finnhub symbols normalized and exchange/country suffix inference improved.
- Provider diagnostics, failure cooldowns and coverage alignment added.
