# Upgrade auf Global Stock Ranker 3.1

## Wichtig vor dem Datei-Update

1. In der laufenden Version 3.0 den Tab **💾 Backup** öffnen.
2. **Komplettes Daten-Backup herunterladen** und auf dem iPhone sichern.
3. Erst danach die Dateien aus diesem 3.1-Paket in GitHub ersetzen.

Streamlit Community Cloud kann lokalen Cache bei einem Redeploy verlieren. Wenn das passiert, lässt sich das zuvor gesicherte ZIP im Backup-Tab wieder einspielen.

## In GitHub ersetzen

Diese Dateien ersetzen:

- `app.py`
- `settings.py`
- `universe.py`
- `pipeline.py`
- `data_sources.py`
- `scoring.py`
- `signals.py`
- `history.py`
- `demo.py`
- `requirements.txt`

Zusätzlich können `CHANGELOG.md`, `VALIDATION.md`, `VERSION.txt`, `README_IPHONE_OHNE_TERMINAL.md` und `tests/test_core.py` aktualisiert werden.

`fmp.py` gehört weiterhin nicht mehr zur App.

## Was 3.1 repariert

- `BP..L`, `RR..L`, `BA..L` werden zu `BP.L`, `RR.L`, `BA.L`.
- Börsen-/Länder-Fallbacks ergänzen u. a. `.AX`, `.PA`, `.CO`, `.ST`, `.HE`, `.SA`.
- Ein alter Universe-Cache wird beim Laden automatisch mit der neuen Symbolzuordnung neu berechnet.
- Wenn sich ein Provider-Symbol durch das Upgrade geändert hat, wird ein früherer 404-Fehler sofort erneut versucht.
- Andere Provider-Fehler erhalten 7 Tage Cooldown und blockieren nicht mehr jeden folgenden Batch.
- Ranking-Abdeckung und Top-100 verwenden dieselben Vollständigkeits-/Confidence-Schwellen.
- Datenqualität zeigt pro Fundamentals-Provider: Cache-Zeilen, nutzbare Zeilen, Fehler, Rate-Limits und letzte Aktualisierung.
- Streamlit-Deprecation `use_container_width` wurde entfernt.

## Nach dem Redeploy

1. Falls nötig: Backup im Tab **💾 Backup** wiederherstellen.
2. **1 · Weltuniversum aktualisieren** ist optional, weil der bestehende Cache bereits automatisch neu gemappt wird; ein frischer Lauf ist trotzdem sinnvoll.
3. **2 · Kurs & Trend aktualisieren** zunächst mit `Top 500`.
4. **3 · SEC-US-Fundamentals aktualisieren**.
5. **4 · Fehlende Fundamentals ergänzen** zunächst mit Finnhub 50–100 Aktien.
6. Danach einen Yahoo-Fallback-Lauf mit 50–100 Aktien durchführen.
7. Im Tab **🧪 Datenqualität** prüfen, wie viele Zeilen wirklich `Nutzbar` sind und ob Fehler/Rate-Limits auftreten.

Die Standardfilter in 3.1 starten bei **55 % Datenvollständigkeit** und **60 % Data Confidence**. Sie können später strenger gestellt werden, wenn die globale Abdeckung gewachsen ist.
