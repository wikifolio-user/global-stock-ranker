from __future__ import annotations

import io
import os
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from demo import make_demo_universe
from fmp import FMPClient, FMPError, build_global_snapshot
from history import add_history_deltas, append_snapshot, load_history, merge_imported_history, symbol_history
from scoring import add_scores, category_from_score
from signals import add_research_signals
from settings import DEFAULT_MIN_COMPLETENESS, DEFAULT_MIN_MARKET_CAP_BN, HISTORY_KEEP_DAYS, HISTORY_TOP_N, TOP_N


load_dotenv()
CACHE_DIR = Path(".cache")
CACHE_DIR.mkdir(exist_ok=True)
SNAPSHOT = CACHE_DIR / "global_snapshot.csv.gz"
HISTORY = Path(os.getenv("HISTORY_PATH", str(CACHE_DIR / "ranking_history.csv.gz")))

st.set_page_config(page_title="Global Stock Ranker", page_icon="📈", layout="wide", initial_sidebar_state="collapsed")

# Mobile-first polish. Streamlit remains usable on desktop while cards collapse naturally on iPhone.
st.markdown(
    """
    <style>
      .block-container {padding-top: 1.0rem; padding-bottom: 3rem; max-width: 1450px;}
      [data-testid="stMetric"] {border: 1px solid rgba(128,128,128,.18); padding: .65rem; border-radius: .85rem;}
      [data-testid="stMetricValue"] {font-size: 1.45rem;}
      div[data-testid="stVerticalBlockBorderWrapper"] {border-radius: 1rem;}
      .small-note {font-size:.82rem; opacity:.72;}
      @media (max-width: 768px) {
        .block-container {padding-left: .75rem; padding-right: .75rem; padding-top: .5rem;}
        h1 {font-size: 1.75rem !important;}
        h2 {font-size: 1.35rem !important;}
        h3 {font-size: 1.15rem !important;}
        [data-testid="stMetricValue"] {font-size: 1.22rem;}
        button[kind="secondary"], button[kind="primary"] {min-height: 2.65rem;}
      }
    </style>
    """,
    unsafe_allow_html=True,
)


def pct(v, digits: int = 1):
    return "—" if pd.isna(v) else f"{float(v):.{digits}%}"


def multiple(v):
    return "—" if pd.isna(v) else f"{float(v):.1f}x"


def signed(v, suffix=""):
    return "—" if pd.isna(v) else f"{float(v):+.1f}{suffix}"


def money(v):
    if pd.isna(v):
        return "—"
    v = float(v)
    if abs(v) >= 1e12:
        return f"{v/1e12:.2f} Bio."
    if abs(v) >= 1e9:
        return f"{v/1e9:.1f} Mrd."
    if abs(v) >= 1e6:
        return f"{v/1e6:.1f} Mio."
    return f"{v:,.0f}"


def price(v, currency=""):
    if pd.isna(v):
        return "—"
    return f"{float(v):,.2f} {currency}".strip()


def safe_series(df: pd.DataFrame, col: str) -> pd.Series:
    if col in df.columns:
        return df[col]
    return pd.Series(np.nan, index=df.index)


def save_snapshot(df: pd.DataFrame):
    df.to_csv(SNAPSHOT, index=False, compression="gzip")


def load_snapshot() -> pd.DataFrame | None:
    if SNAPSHOT.exists():
        return pd.read_csv(SNAPSHOT)
    return None


@st.cache_data(show_spinner=False)
def demo_data() -> pd.DataFrame:
    return make_demo_universe()


@st.cache_data(ttl=900, show_spinner=False)
def cached_quotes(api_key_value: str, symbols: tuple[str, ...]) -> pd.DataFrame:
    if not api_key_value or not symbols:
        return pd.DataFrame()
    return FMPClient(api_key_value).batch_quote(list(symbols))


@st.cache_data(ttl=3600, show_spinner=False)
def cached_detail_bundle(api_key_value: str, symbol: str) -> dict[str, pd.DataFrame]:
    client = FMPClient(api_key_value)
    bundle: dict[str, pd.DataFrame] = {}
    calls = {
        "profile": lambda: client.profile(symbol),
        "estimates": lambda: client.analyst_estimates(symbol),
        "price_change": lambda: client.stock_price_change(symbol),
        "price_target": lambda: client.price_target_consensus(symbol),
        "grades": lambda: client.grades_summary(symbol),
    }
    for key, fn in calls.items():
        try:
            bundle[key] = fn()
        except Exception:
            bundle[key] = pd.DataFrame()
    return bundle


st.title("📈 Global Stock Ranker")
st.caption(
    "Top-100-Research weltweit · Qualität + Wachstum + Cashflow + Bilanz + Bewertung + Verlauf. "
    "Die Signale priorisieren Analysearbeit und sind keine individuelle Kauf-/Verkaufsempfehlung."
)

with st.sidebar:
    st.header("Daten & Filter")
    env_key = os.getenv("FMP_API_KEY", "")
    api_key = st.text_input("FMP API-Key", value=env_key, type="password", help="Am besten als Streamlit Secret FMP_API_KEY speichern.")
    mode = st.radio("Datenmodus", ["Live / Cache", "Demo"], horizontal=False)
    min_mcap_bn = st.number_input("Min. Marktkapitalisierung (Mrd.)", min_value=0.0, value=DEFAULT_MIN_MARKET_CAP_BN, step=0.5)
    min_completeness = st.slider("Min. Datenvollständigkeit", 0, 100, DEFAULT_MIN_COMPLETENESS, 5)
    exclude_financial_like = st.checkbox(
        "Finanzwerte heuristisch herausfiltern",
        value=True,
        help="Banken/Versicherer benötigen ein eigenes Bewertungsmodell.",
    )
    load_quotes = st.checkbox("Kurs-/Trenddaten für Top 100 laden", value=True)
    auto_daily_refresh = st.checkbox("Einmal täglich automatisch aktualisieren", value=True, help="Wenn ein API-Key vorhanden ist, wird beim ersten Öffnen eines neuen Tages ein frischer Fundamentalsnapshot geladen.")
    force_refresh = st.button("Fundamentaldaten neu laden", use_container_width=True)

    st.divider()
    st.subheader("Veränderungshistorie")
    history_upload = st.file_uploader("Historie importieren", type=["csv", "gz"], help="Zum Wiederherstellen nach Cloud-Neustarts.")
    st.caption("Die App speichert tägliche Snapshots lokal. Auf Community-Cloud kann lokaler Speicher bei Neustarts verloren gehen – daher regelmäßig exportieren.")


def get_live_data() -> tuple[pd.DataFrame, bool, str]:
    """Return dataframe, whether a fresh API snapshot was fetched, and source label."""
    cache_is_today = SNAPSHOT.exists() and datetime.fromtimestamp(SNAPSHOT.stat().st_mtime).date() == datetime.now().date()
    if not force_refresh:
        cached = load_snapshot()
        if cached is not None and not cached.empty and (not auto_daily_refresh or cache_is_today or not api_key):
            return cached, False, "Cache"
    if not api_key:
        cached = load_snapshot()
        if cached is not None and not cached.empty:
            return cached, False, "Cache"
        raise FMPError("Für den ersten Live-Abruf wird ein FMP API-Key benötigt.")
    client = FMPClient(api_key)
    with st.spinner("Globale Fundamentaldaten werden geladen …"):
        df = build_global_snapshot(client)
    save_snapshot(df)
    return df, True, "Live"


try:
    if mode == "Demo":
        raw, fresh_data, data_source = demo_data(), False, "Demo"
    else:
        raw, fresh_data, data_source = get_live_data()
except Exception as exc:
    st.error(str(exc))
    st.info("Du kannst auf 'Demo' wechseln, um Oberfläche und Scoring ohne API-Key zu testen.")
    st.stop()

# Load/merge manually backed-up history first.
history = load_history(HISTORY)
if history_upload is not None:
    try:
        compression = "gzip" if str(getattr(history_upload, "name", "")).lower().endswith(".gz") else None
        imported = pd.read_csv(history_upload, compression=compression)
        history = merge_imported_history(history, imported)
        HISTORY.parent.mkdir(parents=True, exist_ok=True)
        history.to_csv(HISTORY, index=False, compression="gzip")
        st.sidebar.success(f"{len(imported):,} Historienzeilen importiert.".replace(",", "."))
    except Exception as exc:
        st.sidebar.error(f"Historie konnte nicht importiert werden: {exc}")

# Fundamental score for the entire universe. Global history rank is deliberately independent of UI filters.
scored_all = add_scores(raw)
scored_all = scored_all.sort_values(["score_total", "data_completeness"], ascending=False).reset_index(drop=True)
scored_all["history_rank"] = np.arange(1, len(scored_all) + 1)

# Only a genuinely refreshed live dataset becomes a new daily history observation.
if mode == "Live / Cache" and fresh_data:
    try:
        history = append_snapshot(scored_all, HISTORY, top_n=HISTORY_TOP_N, keep_days=HISTORY_KEEP_DAYS)
    except Exception as exc:
        st.warning(f"Snapshot konnte nicht gespeichert werden: {exc}")

scored_all = add_history_deltas(scored_all, history)
scored_all["category"] = [category_from_score(s, c) for s, c in zip(scored_all["score_total"], scored_all["data_completeness"])]

# Basic investability/data hygiene filters.
scored = scored_all.copy()
if "marketCap" in scored.columns:
    scored["marketCap"] = pd.to_numeric(scored["marketCap"], errors="coerce")
    scored = scored[scored["marketCap"].fillna(0) >= min_mcap_bn * 1e9]
scored = scored[scored["data_completeness"] >= min_completeness]

if exclude_financial_like:
    if "financialLeverageRatioTTM" in scored.columns:
        lev = pd.to_numeric(scored["financialLeverageRatioTTM"], errors="coerce")
        scored = scored[(lev.isna()) | (lev < 10)]
    if "grossProfitMarginTTM" in scored.columns:
        gm = pd.to_numeric(scored["grossProfitMarginTTM"], errors="coerce")
        scored = scored[(gm.isna()) | ((gm >= 0) & (gm <= 1.2))]

scored = scored.sort_values(["score_total", "data_completeness"], ascending=False).reset_index(drop=True)
scored["rank"] = np.arange(1, len(scored) + 1)
top100 = scored.head(TOP_N).copy()

# Add market-price context only after the fundamental screen; this keeps API traffic small.
if mode != "Demo" and api_key and load_quotes and not top100.empty:
    try:
        with st.spinner("Kurs- und Trenddaten für die Top 100 werden ergänzt …"):
            quotes = cached_quotes(api_key, tuple(top100["symbol"].astype(str).tolist()))
        if not quotes.empty:
            qcols = [c for c in ["symbol", "price", "changePercentage", "volume", "dayLow", "dayHigh", "yearHigh", "yearLow", "priceAvg50", "priceAvg200"] if c in quotes.columns]
            top100 = top100.drop(columns=[c for c in qcols if c != "symbol" and c in top100.columns], errors="ignore")
            top100 = top100.merge(quotes[qcols].drop_duplicates("symbol"), on="symbol", how="left")
    except Exception as exc:
        st.info(f"Kursdaten konnten nicht ergänzt werden: {exc}")

top100 = add_research_signals(top100)

# Header KPIs.
c1, c2, c3, c4 = st.columns(4)
c1.metric("Aktien nach Filter", f"{len(scored):,}".replace(",", "."))
c2.metric("Top-100 Cutoff", f"{top100['score_total'].min():.1f}" if not top100.empty else "—")
roic_series = pd.to_numeric(safe_series(top100, "returnOnInvestedCapitalTTM"), errors="coerce")
fcfy_series = pd.to_numeric(safe_series(top100, "freeCashFlowYieldTTM"), errors="coerce")
c3.metric("Median ROIC", pct(roic_series.median()) if not top100.empty else "—")
c4.metric("Median FCF Yield", pct(fcfy_series.median()) if not top100.empty else "—")
st.markdown(f'<div class="small-note">Datenquelle: {data_source} · Historie: {history["snapshot_date"].nunique() if not history.empty else 0} Snapshot-Tage</div>', unsafe_allow_html=True)

rank_tab, detail_tab, history_tab, methodology_tab = st.tabs(["🏆 Top 100", "🔎 Aktie", "🕘 Historie", "🧭 Methodik"])

with rank_tab:
    st.subheader("Weltweite Top 100")
    if mode == "Demo":
        st.warning("Demo-Modus: Unternehmen, Preise und Kennzahlen sind synthetisch.")

    f1, f2 = st.columns([2, 1])
    query = f1.text_input("Suche", placeholder="Ticker oder Unternehmen", label_visibility="collapsed")
    view = f2.radio("Ansicht", ["Mobil", "Tabelle"], horizontal=True, label_visibility="collapsed")

    filtered_top = top100.copy()
    if query:
        q = query.lower().strip()
        names = safe_series(filtered_top, "name").fillna("").astype(str).str.lower()
        symbols = filtered_top["symbol"].astype(str).str.lower()
        filtered_top = filtered_top[names.str.contains(q, regex=False) | symbols.str.contains(q, regex=False)]

    filter_cols = st.columns(2)
    trend_options = sorted([x for x in filtered_top.get("trend", pd.Series(dtype=str)).dropna().unique().tolist()])
    focus_options = sorted([x for x in filtered_top.get("research_focus", pd.Series(dtype=str)).dropna().unique().tolist()])
    trend_filter = filter_cols[0].multiselect("Trend filtern", trend_options, placeholder="Alle Trends")
    focus_filter = filter_cols[1].multiselect("Research-Fokus", focus_options, placeholder="Alle Setups")
    if trend_filter:
        filtered_top = filtered_top[filtered_top["trend"].isin(trend_filter)]
    if focus_filter:
        filtered_top = filtered_top[filtered_top["research_focus"].isin(focus_filter)]

    if view == "Tabelle":
        display = pd.DataFrame({
            "Rang": filtered_top["rank"],
            "Ticker": filtered_top["symbol"],
            "Unternehmen": safe_series(filtered_top, "name").fillna(filtered_top["symbol"]),
            "Score": filtered_top["score_total"].round(1),
            "Trend": filtered_top["trend"],
            "Score Δ": filtered_top["score_delta"].round(1),
            "Rang Δ": filtered_top["rank_delta"].round(0),
            "Einstieg-Setup": filtered_top["entry_setup_score"].round(0),
            "These-Risiko": filtered_top["thesis_risk_score"].round(0),
            "Research-Fokus": filtered_top["research_focus"],
            "Bewertung": filtered_top["valuation_band"],
            "Kurs-Trend": filtered_top["technical_trend"],
            "ROIC": safe_series(filtered_top, "returnOnInvestedCapitalTTM").map(pct),
            "FCF Yield": safe_series(filtered_top, "freeCashFlowYieldTTM").map(pct),
            "EPS-Wachstum": safe_series(filtered_top, "epsGrowth").map(pct),
            "Net Debt/EBITDA": safe_series(filtered_top, "netDebtToEBITDATTM").map(multiple),
            "KGV": safe_series(filtered_top, "priceToEarningsRatioTTM").map(multiple),
        })
        st.dataframe(display, hide_index=True, use_container_width=True, height=650)
    else:
        page_size = 20
        pages = max(1, int(np.ceil(len(filtered_top) / page_size)))
        page_no = st.number_input("Seite", min_value=1, max_value=pages, value=1, step=1) if pages > 1 else 1
        page_df = filtered_top.iloc[(int(page_no)-1)*page_size:int(page_no)*page_size]
        for _, row in page_df.iterrows():
            with st.container(border=True):
                left, right = st.columns([3, 1])
                name = row.get("name", row["symbol"])
                left.markdown(f"**#{int(row['rank'])} · {row['symbol']} · {name}**")
                left.caption(f"{row['trend']} · {row['research_focus']} · Bewertung: {row['valuation_band']}")
                right.metric("Score", f"{row['score_total']:.1f}", delta=signed(row.get("score_delta")))
                a, b, c = st.columns(3)
                a.metric("Einstieg-Setup", f"{row['entry_setup_score']:.0f}/100")
                b.metric("These-Risiko", f"{row['thesis_risk_score']:.0f}/100")
                c.metric("Rang Δ", signed(row.get("rank_delta")))
                st.caption(
                    f"ROIC {pct(row.get('returnOnInvestedCapitalTTM'))} · FCF Yield {pct(row.get('freeCashFlowYieldTTM'))} · "
                    f"EPS {pct(row.get('epsGrowth'))} · Net Debt/EBITDA {multiple(row.get('netDebtToEBITDATTM'))} · "
                    f"KGV {multiple(row.get('priceToEarningsRatioTTM'))}"
                )

    st.download_button(
        "Top 100 als CSV sichern",
        data=top100.to_csv(index=False).encode("utf-8"),
        file_name=f"global_stock_top100_{datetime.now().date().isoformat()}.csv",
        mime="text/csv",
        use_container_width=True,
    )

with detail_tab:
    if top100.empty:
        st.info("Keine Aktien erfüllen die aktuellen Filter.")
    else:
        labels = {f"#{int(r['rank'])} · {r['symbol']} · {r.get('name', r['symbol'])}": i for i, r in top100.iterrows()}
        label = st.selectbox("Aktie auswählen", list(labels.keys()))
        row = top100.loc[labels[label]]
        currency = str(row.get("currency", "") or "")

        st.subheader(f"{row.get('name', row['symbol'])} ({row['symbol']})")
        st.caption(f"{row['trend']} · {row['research_focus']} · Datenvollständigkeit {row['data_completeness']:.0f}%")

        a, b, c, d = st.columns(4)
        a.metric("Gesamtscore", f"{row['score_total']:.1f}/100", delta=signed(row.get("score_delta")))
        b.metric("Einstieg-Setup", f"{row['entry_setup_score']:.0f}/100")
        c.metric("These-Risiko", f"{row['thesis_risk_score']:.0f}/100")
        d.metric("Rang", f"#{int(row['rank'])}", delta=signed(row.get("rank_delta")))

        st.markdown("#### Entscheidungsbausteine")
        score_table = pd.DataFrame({
            "Bereich": ["Qualität", "Wachstum", "Free Cashflow", "Bilanz", "Kapitalallokation", "Moat-Proxy", "Bewertung", "Risiko"],
            "Punkte": [row["score_quality"], row["score_growth"], row["score_cashflow"], row["score_balance"], row["score_capital_allocation"], row["score_moat"], row["score_valuation"], row["score_risk"]],
            "Maximum": [20, 15, 15, 10, 10, 10, 15, 5],
        })
        score_table["Erfüllung %"] = score_table["Punkte"] / score_table["Maximum"] * 100
        st.bar_chart(score_table.set_index("Bereich")["Erfüllung %"], horizontal=True)

        a, b, c, d = st.columns(4)
        a.metric("ROIC", pct(row.get("returnOnInvestedCapitalTTM")))
        b.metric("FCF Yield", pct(row.get("freeCashFlowYieldTTM")))
        c.metric("FCF-Wachstum", pct(row.get("fcfGrowth")))
        d.metric("EPS-Wachstum", pct(row.get("epsGrowth")))
        e, f, g, h = st.columns(4)
        e.metric("FCF-Marge", pct(row.get("fcfMarginTTM")))
        f.metric("Net Debt / EBITDA", multiple(row.get("netDebtToEBITDATTM")))
        g.metric("KGV", multiple(row.get("priceToEarningsRatioTTM")))
        h.metric("Verwässerung", pct(row.get("sharesGrowth")))

        if pd.notna(row.get("price")):
            st.markdown("#### Kursstruktur")
            a, b, c, d = st.columns(4)
            a.metric("Kurs", price(row.get("price"), currency))
            b.metric("50-Tage-Linie", price(row.get("priceAvg50"), currency))
            c.metric("200-Tage-Linie", price(row.get("priceAvg200"), currency))
            d.metric("52W Hoch Abstand", pct(row.get("distance_52w_high")))
            st.caption(f"Technischer Kontext: **{row['technical_trend']}**. Er dient nur als Timing-/Risikokontext und ersetzt keine Fundamentalanalyse.")

        st.markdown("#### These-Monitor")
        if row.get("thesis_risk_score", 0) >= 60:
            st.error(f"Erhöhtes These-Risiko: {row['thesis_flags']}")
        elif row.get("thesis_risk_score", 0) >= 35:
            st.warning(f"Beobachtung nötig: {row['thesis_flags']}")
        else:
            st.success(f"Aktuell keine starken quantitativen These-Brüche: {row['thesis_flags']}")
        if row.get("red_flag_penalty", 0) > 0:
            st.caption(f"Red-Flag-Abzug im Hauptscore: {row['red_flag_penalty']:.1f} Punkte.")

        # Show what changed versus the previous fundamental snapshot.
        if pd.notna(row.get("prev_score_total")):
            st.markdown("#### Veränderung zum vorherigen Snapshot")
            change_rows = []
            for title, cur_col, prev_col in [
                ("Gesamtscore", "score_total", "prev_score_total"),
                ("Qualität", "score_quality", "prev_score_quality"),
                ("Wachstum", "score_growth", "prev_score_growth"),
                ("Cashflow", "score_cashflow", "prev_score_cashflow"),
                ("Bewertung", "score_valuation", "prev_score_valuation"),
                ("Red-Flag-Abzug", "red_flag_penalty", "prev_red_flag_penalty"),
            ]:
                if prev_col in row.index and pd.notna(row.get(prev_col)):
                    change_rows.append({"Bereich": title, "Vorher": row.get(prev_col), "Aktuell": row.get(cur_col), "Δ": row.get(cur_col) - row.get(prev_col)})
            if change_rows:
                st.dataframe(pd.DataFrame(change_rows).round(1), hide_index=True, use_container_width=True)

        if mode != "Demo" and api_key:
            if st.button("Aktuelle Analysten-/Kurskontextdaten laden", use_container_width=True):
                bundle = cached_detail_bundle(api_key, str(row["symbol"]))
                profile = bundle["profile"]
                estimates = bundle["estimates"]
                changes = bundle["price_change"]
                targets = bundle["price_target"]
                grades = bundle["grades"]

                if not profile.empty:
                    st.markdown("##### Unternehmensprofil")
                    cols = [c for c in ["companyName", "sector", "industry", "country", "exchange", "price", "marketCap", "description"] if c in profile.columns]
                    st.dataframe(profile[cols], hide_index=True, use_container_width=True)
                if not changes.empty:
                    st.markdown("##### Kursperformance")
                    cols = [c for c in ["1D", "5D", "1M", "3M", "6M", "ytd", "1Y", "3Y", "5Y"] if c in changes.columns]
                    if cols:
                        perf = changes[cols].T.reset_index()
                        perf.columns = ["Zeitraum", "Veränderung %"]
                        st.dataframe(perf, hide_index=True, use_container_width=True)
                if not estimates.empty:
                    st.markdown("##### Analystenschätzungen")
                    cols = [c for c in ["date", "revenueAvg", "epsAvg", "numAnalystsRevenue", "numAnalystsEps"] if c in estimates.columns]
                    st.dataframe(estimates[cols].sort_values("date"), hide_index=True, use_container_width=True)
                if not targets.empty:
                    st.markdown("##### Analysten-Kurszielkonsens")
                    st.caption("Externer Konsens, kein Bestandteil des Hauptscores.")
                    st.dataframe(targets, hide_index=True, use_container_width=True)
                if not grades.empty:
                    st.markdown("##### Analysten-Rating-Verteilung")
                    st.caption("Externer Sentiment-Kontext, kein Bestandteil des Hauptscores.")
                    st.dataframe(grades, hide_index=True, use_container_width=True)

with history_tab:
    st.subheader("Veränderungshistorie")
    if history.empty:
        st.info("Noch keine gespeicherte Historie. Ein neuer Live-Abruf legt den ersten täglichen Snapshot an.")
    else:
        dates = sorted(history["snapshot_date"].dropna().astype(str).unique().tolist())
        st.caption(f"Gespeichert: {dates[0]} bis {dates[-1]} · {len(dates)} Snapshot-Tage · Top 300 je Tag")

        hist_symbols = history["symbol"].dropna().astype(str).unique().tolist()
        default_symbol = str(top100.iloc[0]["symbol"]) if not top100.empty else hist_symbols[0]
        idx = hist_symbols.index(default_symbol) if default_symbol in hist_symbols else 0
        hist_symbol = st.selectbox("Aktie in Historie", hist_symbols, index=idx)
        hdf = symbol_history(history, hist_symbol)

        if not hdf.empty:
            if "score_total" in hdf.columns:
                chart = hdf[["snapshot_date", "score_total"]].dropna().set_index("snapshot_date")
                st.markdown("##### Gesamtscore")
                st.line_chart(chart)
            if "history_rank" in hdf.columns:
                st.markdown("##### Globaler Rang (kleiner ist besser)")
                st.line_chart(hdf[["snapshot_date", "history_rank"]].dropna().set_index("snapshot_date"))

            component_cols = [c for c in ["score_quality", "score_growth", "score_cashflow", "score_balance", "score_valuation"] if c in hdf.columns]
            if component_cols:
                st.markdown("##### Teil-Scores")
                normalized = hdf[["snapshot_date"] + component_cols].copy()
                max_map = {"score_quality": 20, "score_growth": 15, "score_cashflow": 15, "score_balance": 10, "score_valuation": 15}
                for c in component_cols:
                    normalized[c] = pd.to_numeric(normalized[c], errors="coerce") / max_map[c] * 100
                st.line_chart(normalized.set_index("snapshot_date"))

            cols = [c for c in ["snapshot_date", "history_rank", "score_total", "returnOnInvestedCapitalTTM", "freeCashFlowYieldTTM", "epsGrowth", "fcfGrowth", "netDebtToEBITDATTM", "priceToEarningsRatioTTM", "red_flag_penalty"] if c in hdf.columns]
            st.dataframe(hdf[cols].sort_values("snapshot_date", ascending=False), hide_index=True, use_container_width=True)

        movers = top100.dropna(subset=["score_delta"]).copy() if "score_delta" in top100.columns else pd.DataFrame()
        if not movers.empty:
            st.markdown("##### Größte Veränderungen seit dem vorherigen Snapshot")
            movers = movers.reindex(movers["score_delta"].abs().sort_values(ascending=False).index).head(15)
            st.dataframe(
                pd.DataFrame({
                    "Ticker": movers["symbol"],
                    "Unternehmen": safe_series(movers, "name").fillna(movers["symbol"]),
                    "Trend": movers["trend"],
                    "Score": movers["score_total"].round(1),
                    "Score Δ": movers["score_delta"].round(1),
                    "Rang Δ": movers["rank_delta"].round(0),
                    "These-Risiko": movers["thesis_risk_score"].round(0),
                }),
                hide_index=True,
                use_container_width=True,
            )

        history_bytes = history.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Historie als CSV sichern",
            data=history_bytes,
            file_name=f"stock_ranker_history_{datetime.now().date().isoformat()}.csv",
            mime="text/csv",
            use_container_width=True,
        )

with methodology_tab:
    st.subheader("Wie die App entscheidet")
    st.markdown(
        """
Der **100-Punkte-Hauptscore** bleibt fundamental: Qualität (20), Wachstum (15), Free Cashflow (15), Bilanz (10), Kapitalallokation (10), Moat-Proxy (10), Bewertung (15) und Risiko (5). Harte Red Flags ziehen Punkte ab.

Zusätzlich gibt es drei getrennte Entscheidungsbausteine:

- **Einstieg-Setup (0–100):** gewichtet Bewertung und Free Cashflow stärker als der Hauptscore. Es soll verhindern, dass ein hervorragendes Unternehmen automatisch als attraktiver Einstieg gilt.
- **Trend:** kombiniert die Veränderung des fundamentalen Scores/Rangs mit der Kursstruktur aus Kurs, 50-Tage- und 200-Tage-Linie. Fundamentale Verschlechterung hat Vorrang vor Kursmomentum.
- **These-Risiko (0–100):** steigt bei schrumpfendem FCF/EPS, ROIC unter 10 %, hoher Verschuldung, deutlicher Verwässerung, negativem FCF, Red Flags oder stark fallendem Score. Das ist ein Frühwarnsystem für eine erneute Analyse der Investmentthese.

Der **Research-Fokus** lautet deshalb nicht einfach „Kaufen/Verkaufen“, sondern z. B. *Einstieg analysieren*, *Qualität gut / Bewertung warten*, *Beobachten* oder *These prüfen*. Das hält die Trennung zwischen Datenlage und tatsächlicher Anlageentscheidung sichtbar.
        """
    )
    st.markdown("#### Was für Kaufentscheidungen zusätzlich sinnvoll ist")
    st.write(
        "Einstieg-Setup, FCF Yield, ROIC, Verschuldung, Verwässerung, Bewertung, 52-Wochen-Kontext und Trendbestätigung. "
        "Bei Detailaufruf können außerdem Analystenschätzungen, Kursperformance und Kurszielkonsens als externer Kontext geladen werden."
    )
    st.markdown("#### Was für Verkaufs-/These-Entscheidungen sinnvoll ist")
    st.write(
        "Nicht der Kursrückgang allein, sondern These-Brüche: fallender ROIC, schrumpfender FCF/EPS, steigende Verschuldung, Verwässerung, "
        "negative Cashflows, steigende Red-Flag-Abzüge und ein dauerhaft sinkender Hauptscore."
    )
    st.info(
        "Die Historie speichert standardmäßig die Top 300 des globalen Fundamentalrankings für bis zu 365 Tage. "
        "Auf Streamlit Community Cloud ist der lokale Dateispeicher nicht als dauerhafte Datenbank gedacht; deshalb gibt es Import/Export. "
        "Für eine dauerhaft produktive Version kann HISTORY_PATH auf persistenten Speicher zeigen oder später eine Datenbank angebunden werden."
    )
