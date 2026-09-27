# Global Stock Ranker 3.1 – kostenlos, weltweit, ohne Terminal

Diese Version ersetzt FMP vollständig. Sie kombiniert mehrere kostenlose Quellen und ist so aufgebaut, dass du die Dateien direkt auf dem iPhone in GitHub anlegen oder ersetzen kannst.


## Neu in Version 3.1

- Internationale Symbolzuordnung repariert und erweitert.
- Fehler wie `BP..L`, `RR..L` und `BA..L` werden automatisch korrigiert.
- Alte Universe-Caches werden beim Laden mit der neuen Mapping-Logik aktualisiert.
- Provider-Fehler blockieren nach einem Fehlversuch nicht mehr jeden folgenden Batch.
- Bei einer korrigierten Symbolzuordnung wird trotz altem 404-Cache sofort neu versucht.
- Datenqualitäts-Tab zeigt nutzbare Zeilen, Fehler und Rate-Limits pro Fundamentals-Provider.
- Ranking-Abdeckung entspricht jetzt exakt den aktiven Rankingfiltern.
- Streamlit-Deprecation-Warnungen für `use_container_width` wurden beseitigt.

Vor dem Upgrade aus 3.0 bitte im Tab **Backup** ein komplettes Daten-Backup herunterladen. Details stehen in `UPGRADE_3_1.md`.

## Datenquellen

- **iShares MSCI ACWI**: offizielles weltweites Large-/Mid-Cap-Universum.
- **SEC EDGAR**: offizielle kostenlose US-XBRL-Fundamentaldaten ohne API-Key.
- **Finnhub Basic Financials**: kostenlose globale Ratios/Kennzahlen mit Free-Key.
- **Yahoo/yfinance**: kostenlose Kurs-/Trenddaten und Best-Effort-Fallback für Fundamentals.
- **Eigene Historie**: Score-, Rang- und These-Veränderungen werden in der App gespeichert.

## Dateien, die in dein GitHub-Repository gehören

Lege alle folgenden Dateien im Hauptordner deines Repositorys ab:

- `app.py`
- `settings.py`
- `universe.py`
- `data_sources.py`
- `pipeline.py`
- `scoring.py`
- `signals.py`
- `history.py`
- `demo.py`
- `requirements.txt`

Die README und CHANGELOG sind optional.

## iPhone: Dateien ohne Terminal in GitHub anlegen

1. Öffne dein GitHub-Repository in Safari.
2. Tippe auf **Add file** → **Create new file**.
3. Gib als Dateinamen z. B. `app.py` ein.
4. Öffne parallel die passende Datei aus dem Ordner `copy_text` dieses Pakets.
5. Alles markieren → kopieren → in GitHub einfügen.
6. Unten **Commit changes**.
7. Wiederhole das für die Dateien oben.

Wenn bereits eine Datei existiert: Datei öffnen → Stift-Symbol → gesamten Inhalt ersetzen → **Commit changes**.

## Streamlit Secrets

Öffne in Streamlit Cloud deine App → **Settings** → **Secrets**.

Empfohlen:

```toml
FINNHUB_API_KEY = "DEIN_KOSTENLOSER_FINNHUB_KEY"
SEC_USER_AGENT = "Vorname Nachname deine@email.de"
```

`FINNHUB_API_KEY` ist kostenlos, aber optional. Ohne ihn kann Yahoo als Fallback verwendet werden.

`SEC_USER_AGENT` ist kein Schlüssel. Die SEC erwartet bei automatisierten Abrufen einen identifizierbaren User-Agent mit Kontaktmöglichkeit. Er wird nur beim Abruf von SEC-Daten als HTTP-Header verwendet.

## Erster Aufbau des kostenlosen Datensatzes

In der App links das Seitenmenü öffnen und dann:

1. **Weltuniversum aktualisieren** – lädt die aktuellen ACWI-Positionen.
2. **Kurs & Trend aktualisieren** – zuerst z. B. Top 500; später Global.
3. **SEC-US-Fundamentals aktualisieren** – füllt große Teile der USA in einem Bulk-Lauf.
4. **Fehlende Fundamentals ergänzen** – Finnhub auswählen und z. B. 100 Aktien pro Lauf.

Die App priorisiert bei der internationalen Ergänzung zunächst Nicht-US-Aktien und die größten ACWI-Gewichte. Wiederhole Schritt 4 über mehrere Sitzungen, bis der Rankingstatus von **Vorläufig** Richtung **Global belastbar** geht.

## Warum schrittweise?

Kostenlose APIs haben Limits. Statt diese zu umgehen, speichert die App bereits geladene Kennzahlen und ergänzt nur fehlende Aktien. So entsteht mit der Zeit eine globale Datenbasis ohne kostenpflichtiges FMP-Ultimate-Abo.

## Ranking und neue professionelle Funktionen

Die App enthält:

- 100-Punkte-Fundamentalscore
- Data Confidence 0–100
- Feldbezogene Datenherkunft SEC / Finnhub / Yahoo
- Einstieg-Setup 0–100
- These-Risiko 0–100
- Exit-Watch
- Fundamentaltrend
- 50-/200-Tage-Trend
- 1M/3M/6M/12M-Momentum
- 52-Wochen-Abstand
- DCF Margin-of-Safety-Proxy
- Reverse-DCF: implizites FCF-Wachstum für 10 Jahre
- historische P/E-Einordnung, wenn Finnhub sie liefert
- Veränderungshistorie für Score und Rang
- Quellen-/Länder-Abdeckungsdiagnostik
- Backup und Restore der gesamten lokalen Datenbasis als ZIP

## Backup ist wichtig

Streamlit Community Cloud garantiert keinen dauerhaft erhaltenen lokalen Dateispeicher. Öffne deshalb gelegentlich den Tab **Backup** und lade ein komplettes Daten-Backup herunter. Nach einem Cloud-Neustart kannst du dieses ZIP wieder hochladen.

## Wichtige Methodik-Hinweise

Banken, Versicherer und REITs werden standardmäßig herausgefiltert, weil FCF-/EV-EBITDA-Modelle dort nicht direkt vergleichbar sind. Du kannst den Filter abschalten, solltest diese Branchen dann aber separat beurteilen.

Yahoo/yfinance ist keine offizielle Yahoo-Finance-API und wird daher nur als Best-Effort-Quelle behandelt. Offizielle SEC-Daten erhalten im Confidence-Modell die höchste Quellenqualität.

Der DCF-Wert ist ein Szenario-Proxy mit zentral hinterlegten Annahmen in `settings.py`. Die App erzeugt Research-Hinweise und keine automatischen Orders.

## Spätere Anpassungen

Für normale Anpassungen reicht meistens `settings.py`. Dort kannst du Schwellen, DCF-Annahmen, Einstiegskriterien, These-Risiko und Trendregeln verändern.

- `settings.py`: Schwellen und Gewichte
- `scoring.py`: 100-Punkte-Modell + DCF
- `signals.py`: Einstieg, Trend, Exit-Watch
- `data_sources.py`: SEC/Finnhub/Yahoo-Adapter
- `pipeline.py`: Quellen-Merge, Confidence, Cache
- `app.py`: Oberfläche
