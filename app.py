"""
Qualitaets-Score fuer alle Aktien der "Watchlist Top 50".

Bewertet jede Aktie nach dem 100-Punkte-Schema (Unternehmensqualitaet,
Wachstum, Free Cashflow, Bilanz, Kapitalallokation, Wettbewerbsvorteil,
Bewertung, Chance-Risiko) - ausschliesslich aus Kennzahlen. Punkte, die sich
nicht aus Zahlen ableiten lassen (Moat, Management), werden ueber Kennzahlen
ANGENAEHERT und in der App ausdruecklich als Naeherung gekennzeichnet.

Datenquelle: Yahoo Finance
  - Jahresabschluesse (bis zu 5 Geschaeftsjahre) ueber die
    Fundamentaldaten-Zeitreihe
  - Wochenkurse (fuer die historische Bewertung und das Kursrisiko)
  - Kursuebersicht (aktueller Kurs, Aktienzahl, Forward-KGV) - gebuendelt
  - Analystenschaetzungen (Gewinnrevisionen) fuer die besten Kandidaten

Ablauf je Lauf (taeglich, siehe .github/workflows/qualitaet.yml):
  1. Universum: alle Aktien aller Aktien-Kategorien, je Yahoo-Symbol einmal.
  2. Fundamentaldaten auffrischen - rollierend: pro Lauf hoechstens
     MAX_AKTUALISIERUNG Werte (fehlende zuerst, dann die aeltesten). Nach
     dem ersten Aufbau (~4 Laeufe) ist jeder Wert hoechstens eine Woche alt.
     Zwischengespeichert in 32 Teildateien (GitHub liefert Dateien nur bis
     1 MB direkt aus).
  3. Taeglich: aktuelle Kurse -> Bewertung, Reverse-DCF, Szenarien, Score.
  4. Ranglisten nach state/qualitaet.json (schlank), die ausfuehrliche
     Analyse je Aktie nach state/qualitaet/details_<n>.json - die App laedt
     sie erst, wenn eine Aktie ausgewaehlt wird.
"""
import bisect
import datetime
import json
import math
import statistics
import sys
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import top50_agent as T
from top50_universum import KATEGORIEN

log = T.log

STATE_ERGEBNIS = "state/qualitaet.json"
STATE_CACHE = "state/qualitaet/kennzahlen_{:02d}.json"
STATE_TRENDS = "state/qualitaet/revisionen.json"
STATE_SEKTOREN = "state/qualitaet/sektoren.json"
STATE_VERLAUF = "state/qualitaet/verlauf.json"
STATE_DETAILS = "state/qualitaet/details_{}.json"
STATE_ALLE = "state/qualitaet/alle_{}.json"       # ALLE bewerteten Aktien (Index-Filter, Qualitaet x Kurs)
STATE_GRUPPEN = "state/qualitaet/gruppen.json"    # Kategorien + Indizes -> Symbole
STATE_VERLAUF_INDIZES = "state/qualitaet/verlauf_indizes.json"
ALLE_TEILE = 6
CACHE_TEILE = 32
DETAIL_TEILE = 32             # Details fuer ALLE bewerteten Aktien (auch die der Qualitaet-x-Kurs-Liste)

MAX_AKTUALISIERUNG = 1500      # Fundamentaldaten-Abrufe je Lauf (je 2 Anfragen)
AKTUALISIEREN_NACH_TAGEN = 7
TRENDS_FUER_TOP = 200          # Analystenrevisionen fuer die besten N Kandidaten
TRENDS_CACHE_TAGE = 7
TOP_N = 50
MAX_JSON_BYTES = 900_000

# DCF-Annahmen (bewusst einfach und fuer alle Werte gleich - vergleichbar)
DISKONT = 0.09                 # Kapitalkosten Basisfall
DISKONT_BEAR, DISKONT_BULL = 0.10, 0.085
G_EWIG = 0.025                 # Wachstum nach Jahr 10
JAHRE_DCF = 10

# Finanzwerte (Banken/Versicherungen): Free Cashflow, ROIC und EBITDA sind dort
# nicht sinnvoll definiert -> nicht nach diesem Schema bewertbar
FINANZ_SEKTOREN = {"financials", "financial services", "finance"}

TYPEN = [
    "TotalRevenue", "GrossProfit", "OperatingIncome", "EBIT", "EBITDA", "NormalizedEBITDA",
    "NetIncomeCommonStockholders", "NetIncome", "DilutedEPS", "DilutedAverageShares",
    "OrdinarySharesNumber", "FreeCashFlow", "OperatingCashFlow", "CapitalExpenditure",
    "StockBasedCompensation", "InterestExpense", "TaxRateForCalcs", "TotalDebt",
    "CashCashEquivalentsAndShortTermInvestments", "CashAndCashEquivalents",
    "StockholdersEquity", "InvestedCapital", "Goodwill", "GoodwillAndOtherIntangibleAssets",
    "TotalAssets", "AccountsReceivable", "Inventory", "RepurchaseOfCapitalStock",
    "CashDividendsPaid", "PurchaseOfBusiness", "ReconciledDepreciation",
]

# Unterwaehrungen: Betrag / Faktor = Hauptwaehrung
UNTERWAEHRUNG = {"GBp": 100, "GBX": 100, "ILA": 100, "ZAc": 100, "ZAC": 100, "KWF": 1000}


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------
def r1(x, n=1):
    return None if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))) else round(x, n)


def cagr(a, b, jahre):
    if a is None or b is None or jahre <= 0 or a <= 0 or b <= 0:
        return None
    return (b / a) ** (1 / jahre) - 1


def nach_eur(betrag, waehrung, datum=None):
    """Betrag in Waehrung -> EUR zum Kurs des Tages (bzw. letzten Kurs)."""
    if betrag is None:
        return None
    faktor = UNTERWAEHRUNG.get(waehrung, 1)
    fx = T.fx_reihe(waehrung)
    if fx is None:
        return betrag / faktor
    if fx is False:
        return None
    tage, kurse = fx
    if datum is None:
        pos = len(tage) - 1
    else:
        pos = bisect.bisect_right(tage, datum) - 1
        if pos < 0:
            pos = 0
    return betrag / faktor / kurse[pos] if kurse[pos] > 0 else None


def w(x, ersatz):
    """x, falls vorhanden - sonst ersatz. (Nicht 'x or ersatz': 0.0 ist ein
    gueltiger Wert, z.B. eine perfekt stabile Marge.)"""
    return ersatz if x is None else x


def punkte_stufen(wert, stufen, umgekehrt=False):
    """stufen = [(schwelle, punkte), ...] absteigend nach Guete."""
    if wert is None:
        return 0
    for schwelle, p in stufen:
        if (wert <= schwelle) if umgekehrt else (wert >= schwelle):
            return p
    return 0


# ---------------------------------------------------------------------------
# Datenabruf
# ---------------------------------------------------------------------------
def lade_jahresabschluesse(symbol):
    """{asOfDate: {Typ: Wert}} + Berichtswaehrung, bis zu ~5 Geschaeftsjahre."""
    jetzt = int(time.time())
    daten = T.yahoo_json(
        f"/ws/fundamentals-timeseries/v1/finance/timeseries/{T.quote(symbol)}",
        {"symbol": symbol, "type": ",".join("annual" + t for t in TYPEN),
         "period1": jetzt - 7 * 366 * 86400, "period2": jetzt + 86400,
         "merge": "false", "padTimeSeries": "true", "lang": "en-US", "region": "US"})
    ergebnis = ((daten or {}).get("timeseries") or {}).get("result") or []
    jahre, waehrung = {}, None
    for eintrag in ergebnis:
        typ = ((eintrag.get("meta") or {}).get("type") or [None])[0]
        if not typ:
            continue
        for w in eintrag.get(typ) or []:
            if not w or not w.get("asOfDate"):
                continue
            wert = (w.get("reportedValue") or {}).get("raw")
            if wert is None:
                continue
            jahre.setdefault(w["asOfDate"], {})[typ[len("annual"):]] = float(wert)
            if typ != "annualTaxRateForCalcs" and w.get("currencyCode") and not waehrung:
                waehrung = w["currencyCode"]
    return jahre, waehrung


def kurs_uebersicht(symbole):
    """Aktueller Kurs, Aktienzahl, Forward-KGV u.a. - 50 Symbole je Abruf."""
    ergebnis = {}
    for i in range(0, len(symbole), 50):
        teil = symbole[i:i + 50]
        daten = T.yahoo_json("/v7/finance/quote", {"symbols": ",".join(teil)}, crumb=True)
        for q in ((daten or {}).get("quoteResponse") or {}).get("result") or []:
            if q.get("symbol"):
                ergebnis[q["symbol"]] = q
    return ergebnis


def lade_revision(symbol):
    """EPS-Schaetzung laufendes GJ heute vs. vor 90 Tagen + erwartetes Wachstum."""
    daten = T.yahoo_json(f"/v10/finance/quoteSummary/{T.quote(symbol)}",
                         {"modules": "earningsTrend"}, crumb=True)
    res = (((daten or {}).get("quoteSummary") or {}).get("result") or [None])[0]
    if not res:
        return None
    trend = {t.get("period"): t for t in ((res.get("earningsTrend") or {}).get("trend") or [])}
    aus = {}
    t0 = trend.get("0y") or {}
    epst = t0.get("epsTrend") or {}
    jetzt = (epst.get("current") or {}).get("raw")
    vorher = (epst.get("90daysAgo") or {}).get("raw")
    if jetzt is not None and vorher not in (None, 0):
        aus["rev90"] = r1((jetzt / vorher - 1) * 100)
    for per, feld in (("0y", "g0"), ("+1y", "g1"), ("+5y", "g5")):
        g = ((trend.get(per) or {}).get("growth") or {}).get("raw")
        if g is not None:
            aus[feld] = r1(g * 100)
    return aus


def lade_sektor(symbol):
    daten = T.yahoo_json("/v1/finance/search", {"q": symbol, "quotesCount": 5, "newsCount": 0})
    for q in (daten or {}).get("quotes") or []:
        if q.get("symbol") == symbol:
            return q.get("sectorDisp") or q.get("sector") or "", q.get("industryDisp") or q.get("industry") or ""
    return "", ""


# ---------------------------------------------------------------------------
# Kennzahlen aus den Jahresabschluessen (preisunabhaengig, woechentlich)
# ---------------------------------------------------------------------------
def _g(j, *felder):
    for f in felder:
        if j.get(f) is not None:
            return j[f]
    return None


def kennzahlen(symbol, jahre_roh, waehrung, wochenkurse, kurs_waehrung):
    """Verdichtet die Jahresabschluesse auf alle preisunabhaengigen Kennzahlen.
    None, wenn die Datenbasis nicht fuer eine Bewertung reicht."""
    jahre = []
    for datum in sorted(jahre_roh):
        j = jahre_roh[datum]
        rev = _g(j, "TotalRevenue")
        if rev is None or rev <= 0:
            continue
        ebit = _g(j, "EBIT", "OperatingIncome")
        da = _g(j, "ReconciledDepreciation")
        fcf = _g(j, "FreeCashFlow")
        if fcf is None and j.get("OperatingCashFlow") is not None and j.get("CapitalExpenditure") is not None:
            fcf = j["OperatingCashFlow"] + j["CapitalExpenditure"]
        debt = _g(j, "TotalDebt") or 0.0
        cash = _g(j, "CashCashEquivalentsAndShortTermInvestments", "CashAndCashEquivalents") or 0.0
        eq = _g(j, "StockholdersEquity")
        ic = _g(j, "InvestedCapital")
        if ic is None and eq is not None:
            ic = eq + debt - cash
        steuer = _g(j, "TaxRateForCalcs")
        steuer = 0.21 if steuer is None else min(max(steuer, 0.0), 0.35)
        jahre.append({
            "datum": datum, "rev": rev, "gp": _g(j, "GrossProfit"), "ebit": ebit,
            "ebitda": _g(j, "EBITDA", "NormalizedEBITDA") or (ebit + da if ebit is not None and da else None),
            "ni": _g(j, "NetIncomeCommonStockholders", "NetIncome"), "eps": _g(j, "DilutedEPS"),
            "sh": _g(j, "DilutedAverageShares", "OrdinarySharesNumber"),
            "sh_ende": _g(j, "OrdinarySharesNumber", "DilutedAverageShares"),
            "fcf": fcf, "capex": abs(j["CapitalExpenditure"]) if j.get("CapitalExpenditure") is not None else None,
            "sbc": _g(j, "StockBasedCompensation"), "zins": abs(j["InterestExpense"]) if j.get("InterestExpense") else None,
            "steuer": steuer, "debt": debt, "cash": cash, "eq": eq, "ic": ic,
            "gw": _g(j, "GoodwillAndOtherIntangibleAssets", "Goodwill"), "ta": _g(j, "TotalAssets"),
            "ar": _g(j, "AccountsReceivable"), "inv": _g(j, "Inventory"),
            "rueckkauf": abs(j["RepurchaseOfCapitalStock"]) if j.get("RepurchaseOfCapitalStock") else 0.0,
            "dividende": abs(j["CashDividendsPaid"]) if j.get("CashDividendsPaid") else 0.0,
            "zukauf": abs(j["PurchaseOfBusiness"]) if j.get("PurchaseOfBusiness") else 0.0,
        })
    jahre = jahre[-5:]
    n = len(jahre)
    if n < 3 or sum(1 for j in jahre if j["fcf"] is not None) < 3 \
            or sum(1 for j in jahre if j["ebit"] is not None) < 3 \
            or sum(1 for j in jahre if j["sh"]) < 2:
        return None

    erst, letzt = jahre[0], jahre[-1]
    spanne = n - 1

    # --- Rentabilitaet ---
    roics = []
    for i, j in enumerate(jahre):
        ic_vor = jahre[i - 1]["ic"] if i > 0 else None
        basis = (j["ic"] + ic_vor) / 2 if (j["ic"] and ic_vor and ic_vor > 0) else j["ic"]
        if j["ebit"] is not None and basis and basis > 0:
            roics.append(j["ebit"] * (1 - j["steuer"]) / basis * 100)
    roes = [j["ni"] / j["eq"] * 100 for j in jahre if j["ni"] is not None and j["eq"] and j["eq"] > 0]
    gm = [j["gp"] / j["rev"] * 100 for j in jahre if j["gp"] is not None]
    om = [j["ebit"] / j["rev"] * 100 for j in jahre if j["ebit"] is not None]
    fm = [j["fcf"] / j["rev"] * 100 for j in jahre if j["fcf"] is not None]
    capex_q = [j["capex"] / j["rev"] * 100 for j in jahre if j["capex"] is not None]
    fcf_summe = sum(j["fcf"] for j in jahre if j["fcf"] is not None)
    ni_summe = sum(j["ni"] for j in jahre if j["ni"] is not None)

    # --- Wachstum (je Aktie!) ---
    def reihe_je_aktie(feld):
        return [(i, j[feld] / j["sh"]) for i, j in enumerate(jahre) if j.get(feld) is not None and j["sh"]]

    fcfps = reihe_je_aktie("fcf")
    epsr = [(i, j["eps"]) for i, j in enumerate(jahre) if j["eps"] is not None]
    if len(epsr) < 2:
        epsr = reihe_je_aktie("ni")
    sh = [(i, j["sh"]) for i, j in enumerate(jahre) if j["sh"]]

    def cagr_reihe(r):
        return cagr(r[0][1], r[-1][1], r[-1][0] - r[0][0]) if len(r) >= 2 else None

    g_umsatz = cagr(erst["rev"], letzt["rev"], spanne)
    g_eps = cagr_reihe(epsr)
    g_fcf = cagr_reihe([(i, j["fcf"]) for i, j in enumerate(jahre) if j["fcf"] is not None])
    g_fcfps = cagr_reihe(fcfps)
    g_aktien = cagr_reihe(sh)
    fcfps_steigt = sum(1 for a, b in zip(fcfps, fcfps[1:]) if b[1] > a[1])
    umsatz_steigt = sum(1 for a, b in zip(jahre, jahre[1:]) if b["rev"] > a["rev"])
    umsatz_einbruch = any(b["rev"] < a["rev"] * 0.9 for a, b in zip(jahre, jahre[1:]))

    # --- Bilanz ---
    nd = letzt["debt"] - letzt["cash"]
    nd_ebitda = nd / letzt["ebitda"] if letzt["ebitda"] and letzt["ebitda"] > 0 else None
    nd_ebitda_vor = ((erst["debt"] - erst["cash"]) / erst["ebitda"]
                     if erst["ebitda"] and erst["ebitda"] > 0 else None)
    zinsdeckung = letzt["ebit"] / letzt["zins"] if letzt["zins"] and letzt["ebit"] is not None else None

    # --- Gewinnqualitaet ---
    sbc_q = letzt["sbc"] / letzt["rev"] * 100 if letzt["sbc"] is not None else None
    gw_q = letzt["gw"] / letzt["ta"] * 100 if letzt["gw"] is not None and letzt["ta"] else None
    g_ford = cagr(erst["ar"], letzt["ar"], spanne) if erst["ar"] and letzt["ar"] else None
    g_lager = cagr(erst["inv"], letzt["inv"], spanne) if erst["inv"] and letzt["inv"] else None

    # --- Kapitalallokation ---
    ausschuettung = sum(j["rueckkauf"] + j["dividende"] for j in jahre)
    zukaeufe = sum(j["zukauf"] for j in jahre)

    # --- Zyklik / normalisierter Free Cashflow ---
    om_std = statistics.pstdev(om) if len(om) >= 3 else None
    zyklisch = bool(umsatz_einbruch or (om_std is not None and om_std > 6))
    fm_schnitt = statistics.mean(fm) if fm else None
    fcf_norm = fm_schnitt / 100 * letzt["rev"] if fm_schnitt is not None else letzt["fcf"]
    fcfs = [j["fcf"] for j in jahre if j["fcf"] is not None]
    if zyklisch:
        fcf_basis = fcf_norm                       # Normaljahr statt Spitze/Tal
    else:
        fcf_basis = min(letzt["fcf"], 1.3 * statistics.mean(fcfs)) if letzt["fcf"] is not None else fcf_norm

    # --- historische Bewertung (FCF-Rendite zu den Geschaeftsjahresenden) ---
    hist_rendite, hist_kgv = [], []
    if wochenkurse:
        tage = [t for t, _ in wochenkurse]
        for j in jahre[:-1] if len(jahre) > 1 else []:
            d = datetime.date.fromisoformat(j["datum"])
            pos = bisect.bisect_right(tage, d) - 1
            if pos < 0 or (d - tage[pos]).days > 15 or not j["sh_ende"]:
                continue
            mcap = nach_eur(wochenkurse[pos][1] * j["sh_ende"], kurs_waehrung, d)
            fcf_e = nach_eur(j["fcf"], waehrung, d)
            ni_e = nach_eur(j["ni"], waehrung, d)
            if mcap and mcap > 0 and fcf_e is not None:
                hist_rendite.append(fcf_e / mcap * 100)
            if mcap and ni_e and ni_e > 0:
                hist_kgv.append(mcap / ni_e)

    # --- Kursrisiko (3 Jahre Wochenkurse) ---
    vola, drawdown = None, None
    if wochenkurse and len(wochenkurse) > 60:
        k = [p for _, p in wochenkurse[-156:]]
        renditen = [math.log(b / a) for a, b in zip(k, k[1:]) if a > 0 and b > 0]
        if len(renditen) > 20:
            vola = statistics.pstdev(renditen) * math.sqrt(52) * 100
        spitze, dd = k[0], 0.0
        for p in [p for _, p in wochenkurse[-260:]]:
            spitze = max(spitze, p)
            dd = min(dd, p / spitze - 1)
        drawdown = dd * 100

    return {
        "wae": waehrung, "gj": letzt["datum"], "gj_erst": erst["datum"], "n": n,
        "roic": r1(roics[-1]) if roics else None, "roic_avg": r1(statistics.mean(roics)) if roics else None,
        "roic_min": r1(min(roics)) if roics else None,
        "roic_trend": r1(roics[-1] - roics[0]) if len(roics) >= 2 else None,
        "roe": r1(roes[-1]) if roes else None,
        "gm": r1(gm[-1]) if gm else None, "gm_avg": r1(statistics.mean(gm)) if gm else None,
        "om": r1(om[-1]) if om else None, "om_avg": r1(statistics.mean(om)) if om else None,
        "om_min": r1(min(om)) if om else None, "om_std": r1(om_std),
        "om_trend": r1(om[-1] - om[0]) if len(om) >= 2 else None,
        "fm": r1(fm[-1]) if fm else None, "fm_avg": r1(fm_schnitt),
        "cc": r1(fcf_summe / ni_summe, 2) if ni_summe > 0 else None,
        "capex": r1(statistics.mean(capex_q)) if capex_q else None,
        "g_ums": r1(g_umsatz * 100) if g_umsatz is not None else None,
        "g_eps": r1(g_eps * 100) if g_eps is not None else None,
        "g_fcf": r1(g_fcf * 100) if g_fcf is not None else None,
        "g_fcfps": r1(g_fcfps * 100) if g_fcfps is not None else None,
        "g_akt": r1(g_aktien * 100, 2) if g_aktien is not None else None,
        "fcfps_up": fcfps_steigt, "fcfps_n": max(len(fcfps) - 1, 0),
        "ums_up": umsatz_steigt, "fcf_pos": sum(1 for f in fcfs if f > 0), "fcf_n": len(fcfs),
        "fcfps": r1(fcfps[-1][1], 3) if fcfps else None,
        "nd": nd, "nd_ebitda": r1(nd_ebitda, 2), "nd_ebitda_vor": r1(nd_ebitda_vor, 2),
        "zinsd": r1(zinsdeckung), "sbc": r1(sbc_q), "gw": r1(gw_q),
        "g_ford": r1(g_ford * 100) if g_ford is not None else None,
        "g_lager": r1(g_lager * 100) if g_lager is not None else None,
        "aussch": r1(ausschuettung / fcf_summe * 100) if fcf_summe > 0 else None,
        "zukauf": r1(zukaeufe / fcf_summe * 100) if fcf_summe > 0 else None,
        "zykl": zyklisch,
        # Bewertungsgrundlagen (Berichtswaehrung)
        "rev": letzt["rev"], "ebit": letzt["ebit"], "ebitda": letzt["ebitda"], "ni": letzt["ni"],
        "fcf": letzt["fcf"], "fcf_basis": fcf_basis, "debt": letzt["debt"], "cash": letzt["cash"],
        "sh": letzt["sh_ende"] or letzt["sh"],
        "h_fcfy": r1(statistics.median(hist_rendite), 2) if len(hist_rendite) >= 2 else None,
        "h_kgv": r1(statistics.median(hist_kgv)) if len(hist_kgv) >= 2 else None,
        "vola": r1(vola), "dd": r1(drawdown),
    }


def aktualisiere(symbol):
    """Holt Jahresabschluesse + Wochenkurse und verdichtet sie. Rueckgabe:
    (kennzahlen oder None, grund)."""
    jahre, waehrung = lade_jahresabschluesse(symbol)
    if not jahre:
        return None, "keine Abschlussdaten"
    chart = T.yahoo_chart(symbol, 6 * 366, intervall="1wk")
    wochen = chart["reihe"] if chart else []
    kurs_wae = chart["waehrung"] if chart else ""
    k = kennzahlen(symbol, jahre, waehrung or kurs_wae, wochen, kurs_wae)
    if k is None:
        return None, "zu wenige Geschaeftsjahre"
    k["kurs_wae"] = kurs_wae
    k["kurs_fb"] = wochen[-1][1] if wochen else None      # Ersatzkurs, falls Uebersicht fehlt
    # Kurshistorie fuer "Qualitaet x Kurs": etwa monatliche Punkte ueber gut
    # 3 Jahre (jede 4. Woche, vom juengsten Kurs rueckwaerts) - klein genug
    # fuer den Cache, genau genug fuer 6 Monate / 1 Jahr / 3 Jahre.
    k["kh"] = [[t.toordinal(), round(p, 6)] for t, p in wochen[::-1][:180:4]][::-1]
    k["name_y"] = chart["name"] if chart else ""
    return k, ""


# ---------------------------------------------------------------------------
# Bewertung (taeglich, kursabhaengig)
# ---------------------------------------------------------------------------
def kurs_perf(kurs, waehrung, kh, heute=None):
    """Kursentwicklung in EUR (Wechselkurs des jeweiligen Tages) ueber 6 Monate,
    1 Jahr und 3 Jahre: {tage: prozent}. Der Referenzkurs ist der letzte
    gespeicherte Punkt am oder vor dem Stichtag (hoechstens 35 Tage davor -
    die Punkte liegen rund 4 Wochen auseinander)."""
    heute = heute or datetime.date.today()
    ergebnis = {}
    if not kh or not kurs:
        return ergebnis
    jetzt_eur = nach_eur(kurs, waehrung)
    for tage in (182, 365, 1095):
        stichtag = (heute - datetime.timedelta(days=tage)).toordinal()
        kandidaten = [(t, p) for t, p in kh if t <= stichtag]
        if not kandidaten or stichtag - kandidaten[-1][0] > 35:
            continue
        t, p = kandidaten[-1]
        damals = nach_eur(p, waehrung, datetime.date.fromordinal(t))
        if jetzt_eur and damals and damals > 0:
            ergebnis[tage] = round((jetzt_eur / damals - 1) * 100, 1)
    return ergebnis


def barwert(fcf0, g1, r, jahre=JAHRE_DCF, g_ewig=G_EWIG, g_verlauf="konstant"):
    """Barwert eines FCF-Stroms. g_verlauf 'konstant': g1 alle 10 Jahre;
    'abflachend': g1 fuer 5 Jahre, dann linear bis g_ewig in Jahr 10."""
    pv, f = 0.0, fcf0
    for t in range(1, jahre + 1):
        if g_verlauf == "abflachend" and t > 5:
            g = g1 + (g_ewig - g1) * (t - 5) / (jahre - 5)
        else:
            g = g1
        f *= 1 + g
        pv += f / (1 + r) ** t
    endwert = f * (1 + g_ewig) / (r - g_ewig)
    return pv + endwert / (1 + r) ** jahre, endwert / f


def implizites_wachstum(ev, fcf0):
    """Reverse-DCF: welches FCF-Wachstum p.a. (10 Jahre) rechtfertigt den
    heutigen Unternehmenswert?"""
    if not ev or not fcf0 or fcf0 <= 0 or ev <= 0:
        return None
    lo, hi = -0.30, 0.60
    if barwert(fcf0, lo, DISKONT)[0] > ev:
        return lo
    if barwert(fcf0, hi, DISKONT)[0] < ev:
        return hi
    for _ in range(60):
        mitte = (lo + hi) / 2
        if barwert(fcf0, mitte, DISKONT)[0] < ev:
            lo = mitte
        else:
            hi = mitte
    return (lo + hi) / 2


def bewerte(sym, k, q, rev, stamm):
    """Voller Score eines Werts. k = Kennzahlen (Cache), q = Kursuebersicht,
    rev = Analystenrevision, stamm = Name/Branche/Land."""
    wae, kwae = k["wae"], k.get("kurs_wae") or (q or {}).get("currency") or k["wae"]
    kurs = (q or {}).get("regularMarketPrice") or k.get("kurs_fb")
    aktien = (q or {}).get("sharesOutstanding") or k["sh"]
    if not kurs or not aktien:
        return None
    kursentwicklung = kurs_perf(kurs, kwae, k.get("kh"))
    mcap = nach_eur(kurs * aktien, kwae)
    netto_schuld = nach_eur(k["debt"] - k["cash"], wae)
    fcf_b = nach_eur(k["fcf_basis"], wae)
    ebit, ebitda, ni = nach_eur(k["ebit"], wae), nach_eur(k["ebitda"], wae), nach_eur(k["ni"], wae)
    if not mcap or mcap <= 0 or netto_schuld is None:
        return None
    ev = mcap + netto_schuld

    kgv = mcap / ni if ni and ni > 0 else None
    fkgv = (q or {}).get("forwardPE")
    fkgv = fkgv if fkgv and 0 < fkgv < 200 else None
    ev_ebit = ev / ebit if ebit and ebit > 0 else None
    ev_ebitda = ev / ebitda if ebitda and ebitda > 0 else None
    ev_fcf = ev / fcf_b if fcf_b and fcf_b > 0 else None
    fcfy = fcf_b / mcap * 100 if fcf_b is not None else None
    if fcfy is not None and fcfy > 40:            # fast sicher Daten-/Waehrungsfehler
        return None

    # Referenzwachstum: was das Unternehmen realistisch schafft
    kandidaten = [x for x in (k["g_ums"], k["g_fcfps"], k["g_eps"], (rev or {}).get("g1")) if x is not None]
    g_ref = statistics.median(kandidaten) / 100 if kandidaten else 0.0
    g_ref = min(max(g_ref, -0.05), 0.25)
    peg = None
    if (fkgv or kgv) and g_ref > 0.01:
        peg = (fkgv or kgv) / (g_ref * 100)

    # Reverse DCF
    g_impl = implizites_wachstum(ev, fcf_b)
    if g_impl is None:
        erwartung = "nicht berechenbar (FCF ≤ 0)"
    elif g_impl <= max(0.02, g_ref - 0.03):
        erwartung = "konservativ"
    elif g_impl <= g_ref + 0.02:
        erwartung = "realistisch"
    elif g_impl <= g_ref + 0.07:
        erwartung = "ambitioniert"
    else:
        erwartung = "extrem optimistisch"

    # Szenarien (Wert des Eigenkapitals in EUR)
    szen = {}
    if fcf_b and fcf_b > 0:
        for name, g1, r, basis in (
                ("bear", max(-0.02, 0.4 * g_ref), DISKONT_BEAR,
                 min(fcf_b, (nach_eur(k["fm_avg"] / 100 * k["rev"], wae) if k["fm_avg"] is not None else None) or fcf_b) * 0.9),
                ("base", min(max(0.75 * g_ref, 0.0), 0.15), DISKONT, fcf_b),
                ("bull", min(max(g_ref * 1.1, 0.03), 0.25), DISKONT_BULL, fcf_b)):
            wert, exit_mult = barwert(basis, g1, r, g_verlauf="abflachend")
            szen[name] = {"g": r1(g1 * 100), "r": r1(r * 100), "wert": wert - netto_schuld,
                          "multiple": r1(exit_mult)}
    fair = szen.get("base", {}).get("wert")
    mos = max((1 - mcap / fair) * 100, -200.0) if fair and fair > 0 else None   # -200 = "Kurs >= 3x fairer Wert"
    bear_verlust = (szen["bear"]["wert"] / mcap - 1) * 100 if "bear" in szen else None

    # ---------------- Punkte ----------------
    P, begr = {}, {}

    # 1) Unternehmensqualitaet (20)
    p = punkte_stufen(k["roic_avg"], [(25, 8), (20, 7), (15, 6), (12, 4), (8, 2), (5, 1)])
    p += 3 if (k["roic_min"] or 0) >= 15 and (k["roic_trend"] or 0) >= -3 else \
        2 if (k["roic_trend"] or 0) >= -3 else 1 if (k["roic_trend"] or 0) >= -8 else 0
    p += punkte_stufen(k["om_avg"], [(25, 3), (15, 2), (8, 1)])
    positiv = w(k["om_avg"], 0) >= 5          # Stabilitaet zaehlt nur bei echter Marge
    p += 0 if not positiv else 2 if w(k["om_std"], 99) <= 3 and (k["om_trend"] or 0) >= -2 else \
        1 if (k["om_trend"] or 0) >= -3 else 0
    p += punkte_stufen(k["cc"], [(0.9, 2), (0.7, 1)])
    p += punkte_stufen(k["capex"], [(5, 2), (10, 1)], umgekehrt=True)
    P["qualitaet"] = min(p, 20)

    # 2) Wachstum & Reinvestment Runway (15)
    p = punkte_stufen(k["g_ums"], [(12, 4), (8, 3), (5, 2), (2, 1)])
    p += punkte_stufen(k["g_eps"], [(15, 4), (10, 3), (6, 2), (2, 1)])
    p += punkte_stufen(k["g_fcfps"], [(15, 5), (10, 4), (6, 3), (3, 2), (0, 1)])
    # Runway-Naeherung: waechst UND verdient dabei hohe Kapitalrenditen
    p += 2 if (k["ums_up"] >= k["n"] - 1 and (k["roic_avg"] or 0) >= 15) else \
        1 if k["ums_up"] >= k["n"] - 2 else 0
    P["wachstum"] = min(p, 15)

    # 3) Free Cashflow & Gewinnqualitaet (15)
    p = 3 if k["fcf_pos"] == k["fcf_n"] else 1 if k["fcf_pos"] >= k["fcf_n"] - 1 else 0
    p += 3 if k["fcfps_n"] and k["fcfps_up"] >= k["fcfps_n"] - 1 and (k["g_fcfps"] or 0) > 0 else \
        1 if w(k["g_fcfps"], -1) > 0 else 0
    p += punkte_stufen(k["fm_avg"], [(20, 3), (12, 2), (6, 1)])
    p += punkte_stufen(k["cc"], [(0.9, 2), (0.7, 1)])
    p += 2 if k["sbc"] is None or k["sbc"] <= 2 else 1 if k["sbc"] <= 5 else 0
    schnell = [x for x in (k["g_ford"], k["g_lager"]) if x is not None]
    p += 1 if not schnell or max(schnell) <= (k["g_ums"] or 0) + 5 else 0
    p += 1 if k["gw"] is None or k["gw"] <= 30 else 0
    P["fcf"] = min(p, 15)

    # 4) Bilanz (10)
    nde = k["nd_ebitda"]
    p = 6 if k["nd"] <= 0 else punkte_stufen(nde, [(1, 5), (2, 4), (2.5, 3), (3, 2), (4, 1)], umgekehrt=True)
    p += 4 if k["zinsd"] is None and k["nd"] <= 0 else punkte_stufen(k["zinsd"], [(15, 4), (8, 3), (4, 2), (2, 1)])
    P["bilanz"] = min(p, 10)

    # 5) Management & Kapitalallokation (10) - Naeherung
    p = punkte_stufen(k["g_akt"], [(-2, 5), (0, 4), (1, 3), (2, 2), (4, 1)], umgekehrt=True)
    p += 2 if (k["roic_trend"] or 0) >= 0 else 1 if (k["roic_trend"] or 0) >= -3 else 0
    p += 2 if (k["zukauf"] or 0) <= 25 else 1 if (k["zukauf"] or 0) <= 60 else 0
    p += 1 if (k["aussch"] or 0) >= 20 or (k["g_fcfps"] or 0) >= 10 else 0
    P["management"] = min(p, 10)

    # 6) Wettbewerbsvorteil (10) - Naeherung
    p = 4 if (k["roic_min"] or 0) >= 15 else 2 if (k["roic_avg"] or 0) >= 15 else 1 if (k["roic_avg"] or 0) >= 10 else 0
    p += punkte_stufen(k["gm_avg"], [(60, 3), (40, 2), (25, 1)])
    p += 0 if not positiv else 3 if w(k["om_std"], 99) <= 2 else 2 if w(k["om_std"], 99) <= 4 else \
        1 if w(k["om_std"], 99) <= 6 else 0
    P["moat"] = min(p, 10)

    # 7) Bewertung (15)
    p = punkte_stufen(fcfy, [(8, 5), (6, 4), (4.5, 3), (3, 2), (2, 1)])
    p += punkte_stufen(ev_ebit, [(10, 3), (15, 2), (22, 1)], umgekehrt=True) if ev_ebit else 0
    p += punkte_stufen(peg, [(1, 2), (2, 1)], umgekehrt=True) if peg else 0
    if k["h_fcfy"] and k["h_fcfy"] > 0 and fcfy is not None:
        verh = fcfy / k["h_fcfy"]
        p += 3 if verh >= 1.2 else 2 if verh >= 1.0 else 1 if verh >= 0.8 else 0
    p += {"konservativ": 2, "realistisch": 1}.get(erwartung, 0)
    P["bewertung"] = min(p, 15)

    # 8) Chance-Risiko / Margin of Safety (5)
    p = punkte_stufen(mos, [(30, 4), (15, 3), (0, 2), (-20, 1)])
    p += 1 if bear_verlust is not None and bear_verlust >= -25 else 0
    P["chance"] = min(p, 5)

    # ---------------- Red Flags ----------------
    flags = []
    if k["fcf_pos"] <= k["fcf_n"] / 2:
        flags.append(("Free Cashflow überwiegend negativ", 10))
    if (k["g_akt"] or 0) > 5:
        flags.append((f"Hohe Verwässerung: Aktienanzahl +{k['g_akt']:.1f} % p.a.", 8))
    elif (k["g_akt"] or 0) > 3:
        flags.append((f"Spürbare Verwässerung: Aktienanzahl +{k['g_akt']:.1f} % p.a.", 4))
    if k["nd_ebitda"] is not None and k["nd_ebitda_vor"] is not None and \
            k["nd_ebitda"] - k["nd_ebitda_vor"] > 1.5 and k["nd_ebitda"] > 2:
        flags.append((f"Verschuldung stark gestiegen (Net Debt/EBITDA {k['nd_ebitda_vor']:.1f}x → {k['nd_ebitda']:.1f}x)", 5))
    if (k["roic_trend"] or 0) < -8:
        flags.append((f"ROIC deutlich gesunken ({k['roic_trend']:+.0f} Prozentpunkte)", 4))
    if (k["om_trend"] or 0) < -5:
        flags.append((f"Operative Marge strukturell fallend ({k['om_trend']:+.0f} Prozentpunkte)", 4))
    if (k["sbc"] or 0) > 10:
        flags.append((f"Sehr hohe aktienbasierte Vergütung ({k['sbc']:.0f} % vom Umsatz)", 5))
    if (k["gw"] or 0) > 50:
        flags.append((f"Goodwill/Immaterielles {k['gw']:.0f} % der Bilanzsumme", 2))
    if (k["zukauf"] or 0) > 100 and (k["g_ums"] or 0) > 0:
        flags.append(("Wachstum überwiegend durch Zukäufe (Zukäufe > Free Cashflow)", 4))
    if (k["g_fcfps"] or 0) < -10:
        flags.append((f"FCF je Aktie stark fallend ({k['g_fcfps']:+.0f} % p.a.)", 5))
    if erwartung == "extrem optimistisch" or (ev_fcf and ev_fcf > 70):
        flags.append(("Sehr hohe eingepreiste Erwartungen (Reverse-DCF)", 5))
    abzug = sum(f[1] for f in flags)

    roh = sum(P.values())
    gesamt = max(roh - abzug, 0)
    qual_anteil = max(sum(P[x] for x in ("qualitaet", "wachstum", "fcf", "bilanz", "management", "moat"))
                      - abzug * 0.6, 0) / 80
    bew_anteil = (P["bewertung"] + P["chance"]) / 20
    schwer = any(f[1] >= 8 for f in flags)

    # ---------------- Einordnung ----------------
    if qual_anteil >= 0.70 and bew_anteil >= 0.55 and gesamt >= 65 and not schwer:
        klasse = "prio"
        grund = "Hohe fundamentale Qualität bei einer Bewertung, die eine genauere Prüfung rechtfertigt"
    elif qual_anteil >= 0.62 and not schwer:
        klasse = "beobachten"
        grund = ("Sehr gutes Unternehmen, aber " +
                 ("die Bewertung bietet kaum Sicherheitsmarge" if bew_anteil < 0.55 else
                  "einzelne Kennzahlen trüben das Bild"))
    else:
        klasse = "nicht"
        schwach = min(("Qualität", P["qualitaet"] / 20), ("Wachstum", P["wachstum"] / 15),
                      ("Cashflow", P["fcf"] / 15), ("Bilanz", P["bilanz"] / 10),
                      ("Bewertung", P["bewertung"] / 15), key=lambda x: x[1])[0]
        grund = f"Schwächster Bereich: {schwach}" + (f"; Red Flag: {flags[0][0]}" if flags else "")

    # Staerken (fuer "Warum interessant?")
    staerken = []
    if (k["roic_avg"] or 0) >= 15:
        staerken.append((k["roic_avg"], f"ROIC Ø {k['roic_avg']:.0f} % über {k['n']} Jahre"
                         + (" – nie unter 15 %" if (k["roic_min"] or 0) >= 15 else "")))
    if (k["g_fcfps"] or 0) >= 8:
        staerken.append((k["g_fcfps"] * 1.5, f"FCF je Aktie +{k['g_fcfps']:.0f} % p.a."))
    if k["nd"] <= 0:
        staerken.append((20, "Nettocash-Bilanz"))
    if (k["g_akt"] or 0) <= -1:
        staerken.append((15, f"Aktienanzahl {k['g_akt']:+.1f} % p.a. (Rückkäufe)"))
    if fcfy and fcfy >= 3 and k["h_fcfy"] and fcfy >= k["h_fcfy"] * 1.15:
        staerken.append((18, f"FCF-Rendite {fcfy:.1f} % – über eigenem Schnitt ({k['h_fcfy']:.1f} %)"))
    if mos is not None and mos >= 15:
        staerken.append((mos, f"Sicherheitsmarge {mos:.0f} % zum konservativen fairen Wert"))
    if (k["gm_avg"] or 0) >= 50 and w(k["om_std"], 99) <= 3:
        staerken.append((16, f"Bruttomarge Ø {k['gm_avg']:.0f} %, stabile operative Marge"))
    if erwartung == "konservativ":
        staerken.append((17, "Kurs preist weniger Wachstum ein als historisch erreicht"))
    staerken = [t for _, t in sorted(staerken, reverse=True)[:5]]

    # These-Killer (objektive Kontrollpunkte)
    killer = []
    if k["roic"] is not None:
        grenze = max(10, (k["roic_avg"] or k["roic"]) * 0.6)
        killer.append(f"ROIC fällt dauerhaft unter {grenze:.0f} % (zuletzt {k['roic']:.0f} %)")
    if k["fcfps"] is not None:
        killer.append(f"FCF je Aktie sinkt zwei Geschäftsjahre in Folge (zuletzt {k['fcfps']:.2f} {wae})")
    nde_txt = f"{k['nd_ebitda']:.1f}x" if k["nd_ebitda"] is not None else ("Nettocash" if k["nd"] <= 0 else "–")
    killer.append(f"Net Debt/EBITDA steigt über {max(2.5, (k['nd_ebitda'] or 0) + 1):.1f}x (aktuell {nde_txt})")
    if k["om_min"] is not None:
        killer.append(f"Operative Marge fällt unter {k['om_min'] - 3:.0f} % (Tief der letzten Jahre {k['om_min']:.0f} %)")
    killer.append(f"Aktienanzahl steigt um mehr als 3 % p.a. (bisher {k['g_akt'] or 0:+.1f} % p.a.)")

    # Katalysatoren, soweit aus Zahlen erkennbar
    kat = []
    if (k["om_trend"] or 0) >= 3:
        kat.append(f"Steigende operative Marge ({k['om_trend']:+.0f} Prozentpunkte)")
    if (k["g_akt"] or 0) <= -1:
        kat.append("Laufende Aktienrückkäufe")
    if k["nd_ebitda_vor"] is not None and k["nd_ebitda"] is not None and k["nd_ebitda"] < k["nd_ebitda_vor"] - 0.5:
        kat.append("Schuldenabbau")
    if (rev or {}).get("rev90") is not None and rev["rev90"] > 2:
        kat.append(f"Positive Gewinnrevisionen ({rev['rev90']:+.1f} % in 90 Tagen)")

    # Risiken (Kennzahlen-basiert)
    risiken, rp = [], 0
    if k["zykl"]:
        risiken.append("Zyklisches Geschäft (Umsatzeinbruch oder stark schwankende Marge)"); rp += 2
    if (k["vola"] or 0) > 45:
        risiken.append(f"Hohe Kursschwankung ({k['vola']:.0f} % p.a.)"); rp += 2
    elif (k["vola"] or 0) > 30:
        risiken.append(f"Erhöhte Kursschwankung ({k['vola']:.0f} % p.a.)"); rp += 1
    if (k["dd"] or 0) < -50:
        risiken.append(f"Maximaler Kursrückgang 5 J.: {k['dd']:.0f} %"); rp += 1
    if nde is not None and nde > 2.5:
        risiken.append(f"Verschuldung {nde:.1f}x EBITDA"); rp += 2
    if bear_verlust is not None and bear_verlust < -40:
        risiken.append(f"Bear Case: {bear_verlust:.0f} % unter aktuellem Wert"); rp += 2
    if erwartung in ("ambitioniert", "extrem optimistisch"):
        risiken.append(f"Bewertungsrisiko: eingepreistes FCF-Wachstum {g_impl * 100:.0f} % p.a."); rp += 2
    if kwae not in ("EUR",):
        risiken.append(f"Währungsrisiko ({T.HAUPTWAEHRUNG.get(kwae, kwae)})")
    risiko = "hoch" if rp >= 5 else "mittel" if rp >= 2 else "niedrig"

    revision = None
    if rev and rev.get("rev90") is not None:
        revision = "positiv" if rev["rev90"] > 2 else "negativ" if rev["rev90"] < -2 else "stabil"

    return {
        "s": sym, "name": stamm.get("name") or k.get("name_y") or sym, "br": stamm.get("branche") or "",
        "land": stamm.get("land") or "", "kurs": r1(kurs, 2), "kwae": kwae, "mcap": r1(mcap / 1e9, 2),
        "gj": k["gj"], "gj_erst": k["gj_erst"], "n": k["n"],
        # Tabelle
        "g_ums": k["g_ums"], "g_eps": k["g_eps"], "g_fcf": k["g_fcf"], "g_fcfps": k["g_fcfps"],
        "roic": k["roic_avg"], "roic_l": k["roic"], "roe": k["roe"], "om": k["om_avg"], "om_l": k["om"],
        "gm": k["gm_avg"], "fm": k["fm_avg"], "nde": k["nd_ebitda"], "netcash": k["nd"] <= 0,
        "akt": k["g_akt"], "fkgv": r1(fkgv), "kgv": r1(kgv), "ev_ebit": r1(ev_ebit),
        "ev_ebitda": r1(ev_ebitda), "ev_fcf": r1(ev_fcf), "fcfy": r1(fcfy, 2), "peg": r1(peg, 2),
        "h_fcfy": k["h_fcfy"], "h_kgv": k["h_kgv"],
        # Details
        "cc": k["cc"], "capex": k["capex"], "sbc": k["sbc"], "gw": k["gw"], "zinsd": k["zinsd"],
        "om_std": k["om_std"], "om_trend": k["om_trend"], "roic_min": k["roic_min"],
        "roic_trend": k["roic_trend"], "aussch": k["aussch"], "zukauf": k["zukauf"],
        "zykl": k["zykl"], "vola": k["vola"], "dd": k["dd"],
        "g_ref": r1(g_ref * 100), "g_impl": r1(g_impl * 100) if g_impl is not None else None,
        "erw": erwartung, "mos": r1(mos), "bear": r1(bear_verlust),
        "szen": {n: {"g": v["g"], "r": v["r"], "m": v["multiple"],
                     "pot": r1(max((v["wert"] / mcap - 1) * 100, -100.0))} for n, v in szen.items()},
        "rev": revision, "rev90": (rev or {}).get("rev90"), "g1": (rev or {}).get("g1"),
        "p6": kursentwicklung.get(182), "p12": kursentwicklung.get(365), "p36": kursentwicklung.get(1095),
        # Punkte
        "p": P, "flags": [[t, a] for t, a in flags], "gesamt": gesamt,
        "q_ant": r1(qual_anteil * 100, 0), "b_ant": r1(bew_anteil * 100, 0),
        "moat": "stark" if P["moat"] >= 8 else "mittel" if P["moat"] >= 5 else "schwach" if P["moat"] >= 3 else "gering",
        "mgmt": "gut" if P["management"] >= 8 else "solide" if P["management"] >= 5 else "schwach",
        "bew": "günstig" if P["bewertung"] >= 11 else "fair" if P["bewertung"] >= 7 else
               "ambitioniert" if P["bewertung"] >= 4 else "teuer",
        "risiko": risiko, "risiken": risiken, "klasse": klasse, "grund": grund,
        "staerken": staerken, "killer": killer, "kat": kat,
    }


# ---------------------------------------------------------------------------
# Hauptablauf
# ---------------------------------------------------------------------------
def teil(symbol):
    return zlib.crc32(symbol.encode()) % CACHE_TEILE


def detail_teil(symbol):
    return zlib.crc32(symbol.encode()) % DETAIL_TEILE


# Felder der Tabellenzeile (alles andere steht nur in den Detaildateien)
ZEILEN_FELDER = ("name", "br", "kurs", "kwae", "mcap", "g_ums", "g_eps", "g_fcf", "g_fcfps", "roic",
                 "om", "fm", "nde", "netcash", "akt", "fkgv", "ev_ebit", "fcfy", "moat", "mgmt", "bew",
                 "risiko", "gesamt", "klasse", "grund", "q_ant", "b_ant", "gj")


ALLE_FELDER = ["s", "name", "br", "kurs", "kwae", "mcap", "g_ums", "g_eps", "g_fcf", "g_fcfps", "roic", "om",
               "fm", "nde", "akt", "fkgv", "ev_ebit", "fcfy", "moat", "mgmt", "bew", "risiko", "gesamt",
               "klasse", "q_ant", "b_ant", "mos", "p6", "p12", "p36", "d"]


def zeile(e):
    z = {f: e[f] for f in ZEILEN_FELDER}
    z["d"] = detail_teil(e["s"])
    return z


def universum(heute):
    """{symbol: stammdaten} aller Aktien + {kategorie: [symbole]}."""
    ymap = T.lade_state(T.STATE_YAHOO, {}) or {}
    index_cache = T.lade_state(T.STATE_INDIZES, {}) or {}
    index_vorher = repr(index_cache)
    stamm, je_kat = {}, {}
    isin_map = ymap.setdefault("isin", {})
    neu_gesucht = False
    for key, kat in KATEGORIEN.items():
        if key in ("etf", "wikifolios"):
            continue
        symbole = []
        if kat["quelle"] == "index":
            liste, _ = T.index_mitglieder(kat, index_cache, heute)
            for ticker, name, boerse, standort, sektor in liste:
                sym, _ = T.yahoo_symbol(ticker, boerse, standort)
                sym = ymap.get("ticker", {}).get(sym, sym)
                stamm.setdefault(sym, {"name": T.schoener_name(name), "branche": sektor, "land": standort})
                symbole.append(sym)
        elif isinstance(kat["quelle"], list):
            for isin, name in kat["quelle"]:
                sym = isin_map.get(isin)
                if sym is None and not T.yahoo_aufgegeben():
                    sym = T.yahoo_symbol_suchen(isin, isin[:2]) or T.yahoo_symbol_suchen(name, isin[:2]) or ""
                    isin_map[isin] = sym
                    neu_gesucht = True
                if sym:
                    stamm.setdefault(sym, {"name": name, "branche": "", "land": isin[:2]})
                    symbole.append(sym)
        je_kat[key] = list(dict.fromkeys(symbole))
    if neu_gesucht:
        T.speichere_state(T.STATE_YAHOO, ymap, "qualitaet: isin-zuordnung [skip ci]")
    if repr(index_cache) != index_vorher:
        T.speichere_state(T.STATE_INDIZES, index_cache, "qualitaet: indexlisten [skip ci]")
    return stamm, je_kat, index_cache


def main():
    start = time.monotonic()
    jetzt = datetime.datetime.now(ZoneInfo("Europe/Berlin"))
    heute = jetzt.date()

    stamm, je_kat, index_cache = universum(heute)
    alle = sorted(stamm)
    log.info(f"Universum: {len(alle)} Aktien in {len(je_kat)} Kategorien")

    # Branche fuer Werte ohne iShares-Sektor (feste Listen) - einmalig, gecacht
    sektoren = T.lade_state(STATE_SEKTOREN, {}) or {}
    fehlend = [s for s in alle if not stamm[s]["branche"] and s not in sektoren]
    if fehlend:
        with ThreadPoolExecutor(T.YAHOO_PARALLEL) as pool:
            for s, (sek, ind) in zip(fehlend, pool.map(lade_sektor, fehlend)):
                sektoren[s] = [sek, ind]
        T.speichere_state(STATE_SEKTOREN, sektoren, "qualitaet: sektoren [skip ci]")
    for s in alle:
        if s in sektoren and not stamm[s]["branche"]:
            stamm[s]["branche"] = sektoren[s][1] or sektoren[s][0]
            stamm[s]["sektor"] = sektoren[s][0]

    # Kennzahlen-Cache laden
    teile = [T.lade_state(STATE_CACHE.format(i), {}) or {} for i in range(CACHE_TEILE)]
    cache = {}
    for t in teile:
        cache.update(t)

    def alter(s):
        try:
            return (heute - datetime.date.fromisoformat(cache[s]["stand"])).days
        except Exception:
            return 9999

    def faellig_ab(s):
        c = cache.get(s) or {}
        if c.get("fehlt"):
            return 3
        if c.get("k") and "kh" not in c["k"]:
            return 0                  # aeltere Eintraege ohne Kurshistorie zuerst nachholen
        return AKTUALISIEREN_NACH_TAGEN

    # Reihenfolge beim Aufbau: grosse Standardwerte zuerst, Russell-Nebenwerte
    # zuletzt - so sind die wichtigsten Listen schon nach dem ersten Lauf voll
    vorrang = {}
    for rang, key in enumerate(("aktien", "dividenden", "usa", "europa", "welt", "welt_neben", "em", "nebenwerte", "us_micro")):
        for sym in je_kat.get(key, []):
            vorrang.setdefault(sym, rang)
    faellig = [s for s in alle if alter(s) >= faellig_ab(s)]
    faellig.sort(key=lambda s: (-min(alter(s), 60), vorrang.get(s, 9), s))
    faellig = faellig[:MAX_AKTUALISIERUNG]
    log.info(f"Fundamentaldaten: {len(faellig)} aufzufrischen (von {len(alle)}), "
             f"{sum(1 for s in alle if s in cache)} im Cache")
    geaendert = set()
    with ThreadPoolExecutor(T.YAHOO_PARALLEL) as pool:
        for s, (k, grund) in zip(faellig, pool.map(aktualisiere, faellig)):
            if T.yahoo_aufgegeben():
                break
            eintrag = {"stand": heute.isoformat()}
            if k:
                eintrag["k"] = k
            else:
                eintrag["fehlt"] = grund
            cache[s] = eintrag
            geaendert.add(teil(s))
    for i in sorted(geaendert):
        daten = {s: v for s, v in cache.items() if teil(s) == i and s in stamm}
        T.speichere_state(STATE_CACHE.format(i), daten, f"qualitaet: kennzahlen teil {i} [skip ci]")

    # Bewertbare Werte (Finanzwerte ohne FCF/EBITDA-Logik ausgenommen)
    bewertbar, finanz, ohne = [], [], 0
    for s in alle:
        k = (cache.get(s) or {}).get("k")
        if not k:
            ohne += 1
            continue
        sektor = (stamm[s].get("sektor") or stamm[s]["branche"] or "").lower()
        if sektor in FINANZ_SEKTOREN and (k.get("gm") is None or k.get("ebitda") is None):
            finanz.append(s)
            continue
        bewertbar.append(s)
    log.info(f"Bewertbar: {len(bewertbar)}, Finanzwerte ausgenommen: {len(finanz)}, ohne Daten: {ohne}")

    # Aktuelle Kurse (gebuendelt) und Revisionen fuer die besten Kandidaten
    uebersicht = kurs_uebersicht(bewertbar) if bewertbar else {}
    log.info(f"Kursuebersicht fuer {len(uebersicht)} Werte")
    trends = T.lade_state(STATE_TRENDS, {}) or {}
    ergebnisse = {}
    for s in bewertbar:
        e = bewerte(s, cache[s]["k"], uebersicht.get(s), (trends.get(s) or {}).get("d"), stamm[s])
        if e:
            ergebnisse[s] = e
    kandidaten = sorted(ergebnisse, key=lambda s: -ergebnisse[s]["gesamt"])[:TRENDS_FUER_TOP]

    def trend_alt(s):
        try:
            return (heute - datetime.date.fromisoformat(trends[s]["stand"])).days
        except Exception:
            return 9999

    neu = [s for s in kandidaten if trend_alt(s) >= TRENDS_CACHE_TAGE]
    if neu and not T.yahoo_aufgegeben():
        with ThreadPoolExecutor(T.YAHOO_PARALLEL) as pool:
            for s, d in zip(neu, pool.map(lade_revision, neu)):
                trends[s] = {"stand": heute.isoformat(), "d": d}
        T.speichere_state(STATE_TRENDS, {s: v for s, v in trends.items() if trend_alt(s) < 30},
                          "qualitaet: revisionen [skip ci]")
        for s in neu:                                   # mit Revisionen neu bewerten
            e = bewerte(s, cache[s]["k"], uebersicht.get(s), (trends.get(s) or {}).get("d"), stamm[s])
            if e:
                ergebnisse[s] = e

    # Ranglisten je Kategorie + "Alle Aktien"
    def rangliste(symbole, n):
        return sorted((s for s in symbole if s in ergebnisse),
                      key=lambda s: (-ergebnisse[s]["gesamt"], -(ergebnisse[s]["b_ant"] or 0)))[:n]

    def auswerten(n):
        ausgabe = {"stand": jetzt.isoformat(timespec="minutes"), "reihenfolge": [], "kategorien": {},
                   "werte": {}, "abdeckung": {"universum": len(alle), "mit_kennzahlen": len(alle) - ohne,
                                              "bewertet": len(ergebnisse), "finanzwerte": len(finanz)},
                   "annahmen": {"diskont": DISKONT * 100, "g_ewig": G_EWIG * 100, "jahre": JAHRE_DCF}}
        gruppen = [("alle", "Alle Aktien", alle)] + [
            (k, KATEGORIEN[k]["titel"], je_kat[k]) for k in KATEGORIEN if k in je_kat]
        for key, titel, symbole in gruppen:
            bewertet = [s for s in symbole if s in ergebnisse]
            je_klasse = {kl: rangliste([s for s in bewertet if ergebnisse[s]["klasse"] == kl], n)
                         for kl in ("prio", "beobachten", "nicht")}
            ausgabe["reihenfolge"].append(key)
            ausgabe["kategorien"][key] = {
                "titel": titel, "universum": len(symbole), "bewertet": len(bewertet),
                "klassen": {kl: sum(1 for s in bewertet if ergebnisse[s]["klasse"] == kl)
                            for kl in ("prio", "beobachten", "nicht")},
                "top": rangliste(bewertet, n),
                # je Klasse die besten - damit z.B. die Beobachtungsliste voll
                # ist, auch wenn die Gesamtliste von einer Klasse dominiert wird
                "je_klasse": je_klasse,
            }
            for s in ausgabe["kategorien"][key]["top"] + [x for v in je_klasse.values() for x in v]:
                ausgabe["werte"][s] = zeile(ergebnisse[s])
        return ausgabe

    n = TOP_N
    ausgabe = auswerten(n)
    while len(json.dumps(ausgabe, ensure_ascii=False).encode()) > MAX_JSON_BYTES and n > 15:
        n -= 5
        ausgabe = auswerten(n)
    ausgabe["top_n"] = n

    # Wochenvergleich: Platz vor 7 Tagen je Liste, Neuaufnahmen, Rausgeflogene
    volle = {}
    for key, kat_erg in ausgabe["kategorien"].items():
        symbole = alle if key == "alle" else je_kat[key]
        bewertet = [s for s in symbole if s in ergebnisse]
        volle[f"{key}|top"] = rangliste(bewertet, len(bewertet))
        for kl in ("prio", "beobachten", "nicht"):
            volle[f"{key}|{kl}"] = rangliste([s for s in bewertet if ergebnisse[s]["klasse"] == kl], len(bewertet))
    seit, vergleich = T.wochenvergleich(STATE_VERLAUF, heute, volle,
                                        {s: stamm[s]["name"] for s in stamm}, top_n=n)
    # Rausgeflogene aus einer Einordnungs-Liste: meist nicht "schlechter",
    # sondern in eine andere Einordnung gewechselt - das dazuschreiben
    for v in vergleich.values():
        for r in v["raus"]:
            if r["jetzt"] is None:
                if r["id"] in ergebnisse:
                    r["klasse"] = ergebnisse[r["id"]]["klasse"]
                else:
                    r["grund"] = "nicht mehr bewertet"
    ausgabe["vergleich_seit"] = seit
    ausgabe["vergleich"] = vergleich

    # Index-Filter: Mitglieder je Index (woechentlich, von beiden Agenten
    # geteilt) + Wochenvergleich der Index-Ranglisten
    gruppen = {"stand": ausgabe["stand"], "reihenfolge": ausgabe["reihenfolge"], "kategorien": {}, "indizes": {},
               "regionen": T.INDEX_REGIONEN}
    for key, kat_erg in ausgabe["kategorien"].items():
        gruppen["kategorien"][key] = {"titel": kat_erg["titel"]}
        if key != "alle":
            gruppen["kategorien"][key]["s"] = [s_ for s_ in je_kat[key] if s_ in ergebnisse]
    try:
        indizes = T.index_mitgliedschaften({s_: stamm[s_]["name"] for s_ in stamm}, index_cache, heute)
        volle_idx = {}
        for key, info in indizes.items():
            mitglieder_idx = [s_ for s_ in info.get("s", []) if s_ in ergebnisse]
            gruppen["indizes"][key] = {"titel": info["titel"], "region": info["region"], "s": mitglieder_idx,
                                       "tabelle": info.get("tabelle"), "zugeordnet": len(info.get("s", []))}
            volle_idx[f"i:{key}|top"] = rangliste(mitglieder_idx, len(mitglieder_idx))
            for kl in ("prio", "beobachten", "nicht"):
                volle_idx[f"i:{key}|{kl}"] = rangliste([s_ for s_ in mitglieder_idx
                                                        if ergebnisse[s_]["klasse"] == kl], len(mitglieder_idx))
        _, vergleich_idx = T.wochenvergleich(STATE_VERLAUF_INDIZES, heute, volle_idx,
                                             {s_: stamm[s_]["name"] for s_ in stamm}, top_n=n)
        for v in vergleich_idx.values():
            for r in v["raus"]:
                if r["jetzt"] is None:
                    if r["id"] in ergebnisse:
                        r["klasse"] = ergebnisse[r["id"]]["klasse"]
                    else:
                        r["grund"] = "nicht mehr bewertet"
        ausgabe["vergleich"].update(vergleich_idx)
    except Exception as e:                 # Index-Filter darf den Score nie verhindern
        log.error(f"Index-Zuordnung fehlgeschlagen: {e}", exc_info=True)
    dauer = time.monotonic() - start
    ausgabe["laufzeit_sek"] = round(dauer)
    ausgabe["anfragen"] = T.ZAEHLER["yahoo"]
    ausgabe["gebremst"] = T.ZAEHLER["yahoo_429"]

    if not ergebnisse:
        log.error("Kein einziger Wert bewertet - vorhandenes Ergebnis bleibt unveraendert.")
        sys.exit(1)
    # Details zuerst schreiben: die App soll nie eine Zeile ohne Analyse sehen
    details = {i: {} for i in range(DETAIL_TEILE)}
    for s in ergebnisse:
        details[detail_teil(s)][s] = ergebnisse[s]
    for i, d in details.items():
        T.speichere_state(STATE_DETAILS.format(i), {"stand": ausgabe["stand"], "werte": d},
                          f"qualitaet: details {i} [skip ci]")
    # Gesamtdaten ALLER bewerteten Aktien fuer den Index-Filter und
    # "Qualitaet x Kurs": die App bildet daraus jede beliebige Rangliste.
    # Kompakt als Arrays, verteilt auf ALLE_TEILE Dateien (je < 1 MB).
    teile = {i: [] for i in range(ALLE_TEILE)}
    for s_, e in ergebnisse.items():
        teile[zlib.crc32(s_.encode()) % ALLE_TEILE].append(
            [s_, (e["name"] or s_)[:40], (e.get("br") or "")[:28]] + [e.get(f) for f in ALLE_FELDER[3:-1]]
            + [detail_teil(s_)])
    for i, zeilen in teile.items():
        T.speichere_state(STATE_ALLE.format(i), {"stand": ausgabe["stand"], "felder": ALLE_FELDER, "zeilen": zeilen},
                          f"qualitaet: alle aktien {i} [skip ci]")
    T.speichere_state(STATE_GRUPPEN, gruppen, "qualitaet: gruppen [skip ci]")
    T.speichere_state(STATE_ERGEBNIS, ausgabe, "qualitaet: taeglicher score [skip ci]")
    klassen = ausgabe["kategorien"]["alle"]["klassen"]
    log.info(f"Fertig in {dauer:.0f}s - {len(ergebnisse)} bewertet "
             f"(Priorität {klassen['prio']}, Beobachten {klassen['beobachten']}, Nicht {klassen['nicht']}), "
             f"{T.ZAEHLER['yahoo']} Yahoo-Anfragen, {T.ZAEHLER['yahoo_429']}x gebremst.")


if __name__ == "__main__":
    main()
