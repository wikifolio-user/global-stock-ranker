# Changelog

## 3.1 – Symbol-Mapping & Datenpipeline-Fix

- Internationale Yahoo/Finnhub-Symbole robuster normalisiert (u. a. London, Australien, Frankreich, Nordics, Brasilien).
- Trailing-Dot-Fehler wie `BP..L`, `RR..L` und `BA..L` behoben.
- Fuzzy Exchange-Matching plus konservative Länder-Fallbacks ergänzt.
- Provider-Symbole werden auch aus bestehenden Universe-Caches beim Laden neu berechnet.
- Fehlgeschlagene/zu dünne Provider-Antworten erhalten 7 Tage Cooldown statt in jedem Batch erneut aufzutauchen.
- Provider-Diagnostik zeigt nutzbare Zeilen, Fehler, Rate-Limits und letzte Aktualisierung.
- Batch-Meldungen zeigen Antworten, nutzbare Datensätze und Fehler transparent.
- Ranking-Abdeckung nutzt jetzt exakt dieselben Vollständigkeits-/Confidence-Schwellen wie die sichtbare Rangliste.
- Länderabdeckung verwendet ebenfalls die aktiven Ranking-Schwellen.
- Streamlit `use_container_width` auf `width="stretch"` migriert.
- Standardfilter für den Aufbau des kostenlosen Datensatzes auf 55 % Vollständigkeit / 60 % Confidence gesetzt.

## 3.0 – Free Multi-Source Architecture

- FMP vollständig aus dem Kernsystem entfernt.
- Offizielles iShares-ACWI-Universum als weltweite Aktienbasis.
- SEC-EDGAR-XBRL-Bulk-Screening für US-Unternehmen ohne API-Key.
- Finnhub Basic Financials als kostenloser globaler Fundamentals-Layer.
- Yahoo/yfinance für globale Kurs-, Momentum- und Fallback-Daten.
- Schrittweises Enrichment mit Cache statt teurem Bulk-Abo.
- Feldbezogene Datenherkunft und Data-Confidence-Score.
- Rankingstatus: Vorläufig / Fortgeschritten / Global belastbar.
- DCF Margin-of-Safety-Proxy.
- Reverse-DCF / implizites 10-Jahres-FCF-Wachstum.
- Historical-P/E-Z-Score, soweit Finnhub-Serie verfügbar.
- Exit-Watch zusätzlich zum Einstieg-Setup.
- 1M/3M/6M/12M-Momentum sowie 50/200-Tage-Trend.
- Länder- und Provider-Abdeckungsdiagnostik.
- Komplettes Cache-/Historien-Backup als ZIP mit Restore-Funktion.
- iPhone-optimierte Oberfläche beibehalten und erweitert.
