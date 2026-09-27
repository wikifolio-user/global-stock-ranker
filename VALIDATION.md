# Validation – Version 3.3

- Python syntax compilation for application, refresh engine and background agent.
- Automated tests cover scoring, source precedence, international ticker mapping, Yahoo chart parsing, provider cooldowns, stale-price selection, Finnhub refresh windows and ranking coverage alignment.
- One-click refresh reuses fresh caches and only schedules missing/stale data.
- Yahoo price failures preserve previous valid price timestamps and receive a cooldown.
- Finnhub successful rows have a success timestamp; provider failures use a separate attempt timestamp.
- GitHub Actions agent uses the same refresh engine as the Streamlit app and persists compressed cache files under `data_cache/`.
- Live third-party endpoints are not exercised by the offline test suite; runtime diagnostics remain visible in the app.

- Regression: leerer/alter Kurs-Cache mit naiven Zeitstempeln wird UTC-sicher verglichen.
