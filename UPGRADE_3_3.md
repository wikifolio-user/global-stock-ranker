# Upgrade auf Global Stock Ranker 3.3

Version 3.3 macht aus den vier manuellen Datenläufen einen intelligenten Ein-Klick-Workflow und ergänzt einen optionalen Hintergrund-Agenten über GitHub Actions.

## Vor dem Upgrade

Im bisherigen App-Tab **Backup** einmal ein komplettes Daten-Backup herunterladen. Ein Streamlit-Redeploy kann den lokalen Cache verlieren.

## In GitHub ersetzen / hinzufügen

Ersetzen: `app.py`, `data_sources.py`, `pipeline.py`, `settings.py`, `requirements.txt`, `VERSION.txt`, `tests/test_core.py`.

Neu hinzufügen: `refresh_engine.py`, `persistence.py`, `agent_refresh.py`, `.github/workflows/stock-data-agent.yml`, `.gitignore`, `UPGRADE_3_3.md` und den Ordner `data_cache` (mit `.gitkeep`).

Die übrigen 3.2-Dateien können ebenfalls aus dem Komplettpaket übernommen werden.

## Ein-Klick-Aktualisierung

In der App gibt es jetzt **🔄 Alles intelligent aktualisieren**. Der Ablauf ist automatisch:

1. Universum nur neu laden, wenn älter als 7 Tage.
2. Nur fehlende oder ältere Kurs-/Trenddaten laden; frische Kurse werden nicht erneut abgefragt.
3. SEC-US-Fundamentals nur neu laden, wenn der SEC-Cache älter als 7 Tage ist.
4. Finnhub nur für fehlende oder ältere Fundamentals; gute Daten bleiben 14 Tage gültig, Fehler erhalten Cooldown.
5. Ranking und Historie anschließend neu berechnen.

Der Fortschritt wird während des Laufs in der App angezeigt. Provider-Limits stoppen den Lauf sauber; der nächste Lauf setzt an der offenen Warteschlange fort.

## Dauerhafter Hintergrund-Agent

Der Workflow `.github/workflows/stock-data-agent.yml` läuft alle 6 Stunden. Er prüft dieselben Fälligkeitsregeln. Wenn nichts fällig ist, werden keine neuen Daten geladen und kein Commit erzeugt. Beim erstmaligen Aufbau kann er mehrere Läufe verwenden, bis die verfügbaren Daten abgearbeitet sind.

Damit GitHub Actions auf SEC und Finnhub zugreifen kann, im Repository unter **Settings → Secrets and variables → Actions** dieselben zwei Werte anlegen, die bereits in Streamlit Secrets stehen:

- `FINNHUB_API_KEY`
- `SEC_USER_AGENT`

Der Workflow besitzt `contents: write` nur für das eigene Repository und speichert den aktuellen Datenbestand komprimiert unter `data_cache/`. Dadurch kann Streamlit nach einem Redeploy den letzten dauerhaften Cache automatisch wieder einlesen.

Falls GitHub das Pushen des Workflow-Tokens verhindert, unter **Settings → Actions → General → Workflow permissions** `Read and write permissions` aktivieren.

## Nach dem Upgrade

Streamlit **Reboot app**. Falls dein bisheriger Cache weg ist, dein Backup einmal wiederherstellen. Danach genügt in der normalen Nutzung der Button **Alles intelligent aktualisieren**. Sobald der GitHub-Agent erstmals `data_cache` committed hat, steht zusätzlich ein dauerhafter Cache über Redeploys hinweg zur Verfügung.
