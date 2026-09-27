# Global Stock Ranker – iPhone ohne Terminal

Diese Edition ist absichtlich flach aufgebaut. Alle wichtigen Dateien liegen direkt im Hauptordner des GitHub-Repositories. Du brauchst weder Terminal noch Codespaces.

## Dateien, die in GitHub liegen müssen

1. `app.py` – Oberfläche und Seiten der App
2. `settings.py` – **deine wichtigste Anpassungsdatei** für Schwellen, Gewichte und Top-N
3. `scoring.py` – 100-Punkte-Fundamentalscore
4. `signals.py` – Einstieg-Setup, Trend, These-Risiko und Research-Fokus
5. `history.py` – tägliche Veränderungshistorie
6. `fmp.py` – Verbindung zum Finanzdatenanbieter FMP
7. `demo.py` – Demo-Daten ohne API-Key
8. `requirements.txt` – Python-Abhängigkeiten für Streamlit Cloud

`CHANGELOG.md` und `VERSION.txt` sind optional, aber sinnvoll.

---

# A. Erstmalige Einrichtung nur mit dem iPhone

## 1. Dateien in die iPhone-App „Dateien“ laden

Lade die ZIP `global_stock_ranker_iphone_no_terminal.zip` aus ChatGPT herunter.

Öffne danach auf dem iPhone:

**Dateien → Downloads → global_stock_ranker_iphone_no_terminal.zip**

Tippe einmal auf die ZIP. iOS erstellt daneben automatisch einen entpackten Ordner.

## 2. GitHub-Repository anlegen

Öffne `github.com` in Safari und melde dich an.

Erstelle ein neues Repository, zum Beispiel:

`global-stock-ranker`

Private oder Public funktioniert. Wenn es Private ist, musst du Streamlit später Zugriff auf private Repositories erlauben.

## 3. Dateien ohne Terminal hochladen

Öffne dein Repository in Safari.

Wähle:

**Add file → Upload files**

Tippe auf **choose your files** und wähle in der iPhone-Dateiauswahl die entpackten Dateien aus.

Lade mindestens diese acht Dateien hoch:

- app.py
- settings.py
- scoring.py
- signals.py
- history.py
- fmp.py
- demo.py
- requirements.txt

Danach unten **Commit changes** bestätigen.

Wenn Safari nicht mehrere Dateien auf einmal komfortabel auswählen lässt, lade sie nacheinander hoch.

---

# B. Streamlit Cloud veröffentlichen

1. Öffne `share.streamlit.io` in Safari.
2. Melde dich mit GitHub an.
3. Wähle **Create app**.
4. Wähle dein Repository `global-stock-ranker`.
5. Branch: `main`.
6. Main file path: `app.py`.
7. Öffne **Advanced settings**.
8. Im Feld **Secrets** kannst du deinen FMP-Key sicher hinterlegen:

```toml
FMP_API_KEY = "DEIN_API_KEY"
```

9. Speichern und **Deploy** wählen.

Wichtig: Den echten API-Key niemals in `app.py`, `settings.py` oder eine andere GitHub-Datei schreiben.

---

# C. App wie eine iPhone-App benutzen

Öffne deine `*.streamlit.app`-Adresse in Safari.

Dann:

**Teilen → Zum Home-Bildschirm → Hinzufügen**

Damit erscheint der Stock Ranker als eigenes Icon auf deinem iPhone.

---

# D. Spätere Änderungen ohne Terminal

Das ist der wichtigste Teil dieser Edition.

## Beste Methode: Datei direkt in GitHub bearbeiten

Beispiel: Du willst den Mindestscore für „Einstieg analysieren“ von 75 auf 80 erhöhen.

1. Öffne dein GitHub-Repository.
2. Tippe auf `settings.py`.
3. Tippe auf das **Stift-Symbol / Edit this file**.
4. Suche in `FOCUS` nach:

```python
"entry_total_score": 75,
```

5. Ändere es zu:

```python
"entry_total_score": 80,
```

6. Tippe auf **Commit changes**.

Streamlit erkennt den GitHub-Commit und aktualisiert die veröffentlichte App automatisch.

Du brauchst keinen Terminal-Befehl.

---

# E. Welche Datei ändere ich für was?

## `settings.py`

Hier solltest du zuerst suchen. Diese Datei ist bewusst für einfache Änderungen gedacht.

Dort kannst du ändern:

- Anzahl Top-Aktien
- Länge der Historie
- Mindest-Marktkapitalisierung
- Mindest-Datenvollständigkeit
- Schwellen für hohe Analysepriorität
- Gewichtung des Einstieg-Setups
- Warnschwellen für These-Risiko
- Bedingungen für „Einstieg analysieren“
- Bedingungen für „These prüfen“
- technische 50/200-Tage-Toleranzen

## `scoring.py`

Hier steckt das eigentliche fundamentale 100-Punkte-Modell.

Ändern, wenn du z. B. möchtest:

- ROIC stärker gewichten
- KGV-Grenzen verändern
- FCF Yield stärker gewichten
- Margen-Schwellen ändern
- Verschuldungsgrenzen ändern
- zusätzliche Red Flags einbauen

## `signals.py`

Ändern für:

- Trendlogik
- Einstieg-Setup
- These-Risiko
- Research-Fokus
- zusätzliche Kauf-/Verkaufs-Warnsignale

## `app.py`

Ändern für:

- Texte
- iPhone-Darstellung
- neue Tabellen/Spalten
- Filter
- Seiten und Buttons

## `history.py`

Ändern für:

- Veränderungshistorie
- Vergleich zum vorherigen Snapshot
- Rang- und Score-Verlauf

## `fmp.py`

Nur ändern, wenn sich Daten-Endpunkte ändern oder ein anderer Datenanbieter ergänzt werden soll.

---

# F. Copy/Paste statt Datei-Upload

Im Ordner `copy_text` liegt jede wichtige Datei zusätzlich als `.txt` vor.

Beispiel:

`copy_text/settings.py.txt`

Du kannst den kompletten Inhalt öffnen, kopieren und in GitHub in eine Datei namens `settings.py` einfügen.

Das ist besonders praktisch, wenn ich dir später eine überarbeitete einzelne Datei bereitstelle: Du ersetzt dann nur deren Inhalt in GitHub und bestätigst **Commit changes**.

---

# G. Empfohlener Änderungsablauf

Für spätere Versionen musst du nicht jedes Mal die ganze App ersetzen.

1. Ich nenne dir die geänderten Dateien.
2. Du öffnest genau diese Datei(en) in GitHub.
3. Stift-Symbol antippen.
4. Alten Inhalt vollständig markieren.
5. Neuen Inhalt aus meiner `.txt`-Datei kopieren und einfügen.
6. **Commit changes**.
7. Streamlit aktualisiert die App.

So bleibt deine Änderungshistorie in GitHub erhalten und du kannst frühere Versionen über die Commit-Historie nachvollziehen.

---

# H. Wichtig für die Aktien-Historie

Die App speichert Ranking-Snapshots in ihrem lokalen Speicher und bietet Export/Import der Historie. Bei Cloud-Hosting solltest du die Historie regelmäßig exportieren, da lokaler App-Speicher bei einem Neuaufbau der Cloud-Instanz nicht als dauerhafte Datenbank gedacht ist.

Für eine spätere professionelle Version wäre eine externe Datenbank die sauberere Lösung.
