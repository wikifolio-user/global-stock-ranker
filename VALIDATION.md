# Validation

- Python syntax compilation: passed for all application modules.
- Automated core tests: **3 passed**.
- Tested areas:
  - iShares CSV parser and international ticker mapping
  - provider precedence / field-level provenance / confidence pipeline
  - 0–100 scoring, Reverse-DCF proxy, entry score, thesis risk and exit-watch bounds

Live HTTP calls cannot be executed in the build container, so provider adapters are defensive, cached and expose source/coverage diagnostics in the UI.
