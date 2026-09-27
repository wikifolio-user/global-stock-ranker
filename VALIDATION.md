# Validation – Version 3.1

- Python syntax compilation: passed for all application modules and tests.
- Automated core tests: **7 passed**.
- Tested areas:
  - iShares CSV parser and US/Hong-Kong mapping
  - Regression mappings for London trailing dots (`BP.`, `RR.`, `BA.`)
  - Australia / France / Denmark / Sweden / Finland / Brazil suffix mapping
  - Provider precedence / field-level provenance / confidence pipeline
  - 0–100 scoring, Reverse-DCF proxy, entry score, thesis risk and exit-watch bounds
  - 7-day cooldown for recent provider failures
  - Immediate retry when an app upgrade changes the mapped provider symbol
  - Coverage calculation aligned with the visible ranking thresholds

Live HTTP calls are not executed as part of the offline build validation. Provider adapters remain defensive and the app now exposes provider success/error/rate-limit diagnostics directly in the data-quality tab.
