# GitHub-Datenagent auf dem iPhone installieren

Der Agent ist für dauerhafte Daten wichtig. Ohne ihn speichert Streamlit interaktive Updates nur im Laufzeit-Cache.

## Variante A – Ordnerstruktur aus dem ZIP übernehmen

Wenn dein GitHub-Upload die Ordnerstruktur erhält, muss diese Datei existieren:

`.github/workflows/stock-data-agent.yml`

## Variante B – sicher auf dem iPhone

Falls `.github` beim Upload fehlt:

1. GitHub-Repository öffnen.
2. **Add file → Create new file**.
3. Als Dateinamen exakt eingeben: `.github/workflows/stock-data-agent.yml`
4. Den Inhalt aus `STOCK_DATA_AGENT_WORKFLOW.yml` kopieren und einfügen.
5. **Commit changes**.
6. Unter **Settings → Secrets and variables → Actions** zwei Repository Secrets anlegen:
   - `FINNHUB_API_KEY`
   - `SEC_USER_AGENT`
7. Unter **Settings → Actions → General → Workflow permissions** `Read and write permissions` aktivieren.
8. Oben **Actions → Stock Data Agent → Run workflow** starten.

Nach einem erfolgreichen Lauf müssen im Repository unter `data_cache/` komprimierte Cache-Dateien und `agent_status.json` sichtbar sein.
