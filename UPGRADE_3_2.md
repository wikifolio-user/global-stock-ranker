# Upgrade auf Global Stock Ranker 3.2

## Warum 3.2 nötig ist

Die Live-Logs zeigen massenhaft Yahoo-HTTP-401-Fehler (`Invalid Crumb` / `User is unable to access this feature`) sowie nachfolgende Rate-Limits. Das betrifft vor allem Yahoo `quoteSummary`/`get_info()` und kann auf Streamlit-Cloud-IP-Adressen auftreten.

Version 3.2 trennt deshalb die Aufgaben:

- **SEC EDGAR**: US-Fundamentals
- **Finnhub**: internationale Fundamentals
- **Yahoo v8 Chart**: nur Kurs- und Trendhistorie, ohne QuoteSummary/Crumb
- **Yahoo Fundamentals**: nur bereits vorhandener Alt-Cache, keine neuen Live-Abrufe

## Dateien in GitHub ersetzen

Mindestens ersetzen:

- `app.py`
- `data_sources.py`
- `settings.py`
- `requirements.txt`
- `VERSION.txt`

Empfohlen zusätzlich:

- `CHANGELOG.md`
- `README.md`
- `README_IPHONE_OHNE_TERMINAL.md`
- `VALIDATION.md`
- `tests/test_core.py`

## Danach in Streamlit

1. App über **Manage app → Reboot app** neu starten.
2. Prüfen, dass unten **Version 3.2** steht.
3. Zuerst **Kurs & Trend** mit `Top 250` starten. Wenn stabil, `Top 500`.
4. Danach SEC aktualisieren.
5. Internationale Fundamentals ausschließlich über Finnhub in 50–100er Läufen ergänzen.
6. Im Tab **Datenqualität** Yahoo-Chart-Fehler/Rate-Limits und Finnhub-Nutzbarkeit beobachten.

Die vorhandenen Cache-Dateien müssen nicht gelöscht werden. Gültige alte Werte bleiben erhalten.
