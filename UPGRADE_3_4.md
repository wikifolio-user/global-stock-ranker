# Upgrade auf Global Stock Ranker 3.4

## Wichtigste Änderungen

1. **Ein-Klick-Update setzt fort statt neu anzufangen.** Frische Kurse, SEC-Daten und Finnhub-Antworten werden übersprungen.
2. **Finnhub-Queue-Fix:** auch dünne, aber erfolgreiche Antworten verlassen die Warteschlange für 14 Tage. In älteren Versionen wurden 1–4-Kennzahlen-Antworten immer wieder neu geladen.
3. **Global Lite:** Nicht-US-Aktien können mit einem kleineren, breit gestreuten Kennzahlensatz rankingfähig werden. Global Lite verlangt mehrere Evidenzgruppen und erhält einen deutlichen Ranking-Abschlag.
4. **Agent-Workflow repariert:** `agent_status.json` wird jetzt auch dann committed, wenn nur der Status geändert wurde. Cache + Status werden gemeinsam gestaged.
5. **Queue-Anzeige:** Die Sidebar zeigt, wie viele Kurs- und Finnhub-Aktien noch fällig sind. Nach jedem Smart-Update wird die Restqueue angezeigt.
6. **Finnhub-Symbol-Fallback:** bei fehlenden Basic Financials wird einmal konservativ über Finnhub Symbol Lookup nach einer passenden Börsennotation gesucht.

## Dateien ersetzen

Für ein sauberes Upgrade alle Dateien aus dem Paket in das Repository übernehmen. Besonders wichtig:

- `app.py`
- `pipeline.py`
- `scoring.py`
- `signals.py`
- `refresh_engine.py`
- `data_sources.py`
- `agent_refresh.py`
- `settings.py`
- `.github/workflows/stock-data-agent.yml`

## Danach

- Streamlit: **Manage app → Reboot app**
- GitHub Actions Secrets: `FINNHUB_API_KEY`, `SEC_USER_AGENT`
- GitHub: **Settings → Actions → General → Workflow permissions → Read and write permissions**
- Actions → **Stock Data Agent → Run workflow** einmal manuell starten.

Wenn der Workflow korrekt läuft, erscheinen unter `data_cache/` nach und nach `*.csv.gz` und `agent_status.json`.
