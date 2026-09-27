from __future__ import annotations

import io
import json
import os
import zipfile
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from data_sources import (
    DataSourceError,
    enrich_finnhub_fundamentals,
    fetch_sec_bulk_snapshot,
    fetch_yahoo_price_snapshot,
)
from demo import make_demo_universe
from history import add_history_deltas, append_snapshot, load_history, merge_imported_history, symbol_history
from persistence import bootstrap_local_cache
from refresh_engine import RefreshPolicy, run_refresh_cycle
from pipeline import (
    FUNDAMENTAL_METRICS,
    assemble_snapshot,
    coverage_summary,
    import_cache_bytes,
    load_cache,
    provenance_table,
    provider_batch_stats,
    provider_diagnostics,
    save_cache,
    select_next_symbols,
    upsert_cache,
)
from scoring import add_scores, category_from_score
from settings import (
    APP_VERSION,
    DEFAULT_MIN_COMPLETENESS,
    DEFAULT_MIN_CONFIDENCE,
    EXCLUDE_SPECIAL_SECTORS_DEFAULT,
    FINNHUB_DEFAULT_BATCH,
    HISTORY_KEEP_DAYS,
    HISTORY_TOP_N,
    SPECIAL_SECTORS,
    TOP_N,
)
from signals import add_research_signals
from universe import fetch_acwi_universe, load_universe_cache, save_universe


load_dotenv()
CACHE_DIR = Path(os.getenv("CACHE_DIR", ".cache_v3"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)
PATHS = {
    "universe": CACHE_DIR / "universe.csv.gz",
    "sec": CACHE_DIR / "sec_fundamentals.csv.gz",
    "finnhub": CACHE_DIR / "finnhub_fundamentals.csv.gz",
    "yahoo": CACHE_DIR / "yahoo_fundamentals.csv.gz",
    "prices": CACHE_DIR / "yahoo_prices.csv.gz",
    "history": CACHE_DIR / "ranking_history.csv.gz",
}

# Restore durable repository cache after Streamlit redeploys/reboots. Existing local
# files are kept, so interactive updates in the current process are never overwritten.
PERSISTED_RESTORED = bootstrap_local_cache(PATHS, "data_cache")
AGENT_STATUS_PATH = Path("data_cache/agent_status.json")

st.set_page_config(
    page_title="Global Stock Ranker 3.3",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
      .block-container {padding-top:.8rem; padding-bottom:3rem; max-width:1500px;}
      [data-testid="stMetric"] {border:1px solid rgba(128,128,128,.18); padding:.65rem; border-radius:.85rem;}
      [data-testid="stMetricValue"] {font-size:1.35rem;}
      div[data-testid="stVerticalBlockBorderWrapper"] {border-radius:1rem;}
      .small-note {font-size:.82rem; opacity:.74;}
      .status-pill {display:inline-block; padding:.2rem .55rem; border:1px solid rgba(128,128,128,.25); border-radius:999px; font-size:.82rem; margin-right:.3rem;}
      @media (max-width:768px) {
        .block-container {padding-left:.7rem; padding-right:.7rem; padding-top:.4rem;}
        h1 {font-size:1.65rem !important;} h2 {font-size:1.3rem !important;} h3 {font-size:1.08rem !important;}
        [data-testid="stMetricValue"] {font-size:1.16rem;}
        button[kind="secondary"], button[kind="primary"] {min-height:2.7rem;}
      }
    </style>
    """,
    unsafe_allow_html=True,
)


def pct(v, digits: int = 1) -> str:
    try:
        if pd.isna(v):
            return "—"
        return f"{float(v):.{digits}%}"
    except Exception:
        return "—"


def multiple(v) -> str:
    try:
        if pd.isna(v):
            return "—"
        return f"{float(v):.1f}x"
    except Exception:
        return "—"


def signed(v, suffix="") -> str:
    try:
        if pd.isna(v):
            return "—"
        return f"{float(v):+.1f}{suffix}"
    except Exception:
        return "—"


def price(v, currency="") -> str:
    try:
        if pd.isna(v):
            return "—"
        return f"{float(v):,.2f} {currency}".strip()
    except Exception:
        return "—"


def safe_series(df: pd.DataFrame, col: str) -> pd.Series:
    if col in df.columns:
        return df[col]
    return pd.Series(np.nan, index=df.index)


def weighted_universe_slice(universe: pd.DataFrame, scope: str) -> pd.DataFrame:
    if universe.empty:
        return universe
    work = universe.copy()
    work["_weight"] = pd.to_numeric(work.get("ishares_weight_pct"), errors="coerce").fillna(0)
    work = work.sort_values("_weight", ascending=False)
    if scope.startswith("Top 250"):
        return work.head(250).drop(columns="_weight")
    if scope.startswith("Top 500"):
        return work.head(500).drop(columns="_weight")
    if scope.startswith("Top 1000"):
        return work.head(1000).drop(columns="_weight")
    return work.drop(columns="_weight")


def build_backup_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, path in PATHS.items():
            if path.exists():
                zf.write(path, arcname=f"{name}.csv.gz")
        manifest = (
            f"Global Stock Ranker Backup\nVersion: {APP_VERSION}\n"
            f"Created: {datetime.now().isoformat()}\n"
        )
        zf.writestr("BACKUP_INFO.txt", manifest)
    return buffer.getvalue()


def restore_backup(upload) -> list[str]:
    restored = []
    if upload is None:
        return restored
    raw = upload.getvalue()
    with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
        for name, path in PATHS.items():
            arc = f"{name}.csv.gz"
            if arc in zf.namelist():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(zf.read(arc))
                restored.append(name)
    return restored


def load_live_caches():
    return (
        load_universe_cache(PATHS["universe"]),
        load_cache(PATHS["sec"]),
        load_cache(PATHS["finnhub"]),
        load_cache(PATHS["yahoo"]),
        load_cache(PATHS["prices"]),
    )


st.title("📈 Global Stock Ranker 3.3")
st.caption(
    "Kostenloses Multi-Source-Research · intelligenter Cache · iShares ACWI + SEC EDGAR + Finnhub Free + Yahoo Chart · "
    "Qualität, Wachstum, FCF, Bilanz, Bewertung, DCF-Proxy, Trend und Veränderungshistorie."
)

# ---------------------------------------------------------------------------
# Sidebar / data operations
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("Daten & Ranking")
    mode = st.radio("Datenmodus", ["Live kostenlos", "Demo"], horizontal=False)
    min_completeness = st.slider("Min. Datenvollständigkeit", 0, 100, DEFAULT_MIN_COMPLETENESS, 5)
    min_confidence = st.slider("Min. Data Confidence", 0, 100, DEFAULT_MIN_CONFIDENCE, 5)
    exclude_special = st.checkbox(
        "Finanzwerte & REITs herausfiltern",
        value=EXCLUDE_SPECIAL_SECTORS_DEFAULT,
        help="Das Standardmodell ist für Banken/Versicherer/REITs nicht optimal. Diese benötigen Spezialmodelle.",
    )

    st.divider()
    st.subheader("Kostenlose Quellen")
    finnhub_key = st.text_input(
        "Finnhub API-Key",
        value=os.getenv("FINNHUB_API_KEY", ""),
        type="password",
        help="Kostenloser Key. Für internationale Fundamentaldaten empfohlen.",
    )
    sec_user_agent = st.text_input(
        "SEC User-Agent",
        value=os.getenv("SEC_USER_AGENT", ""),
        placeholder="Name deine@email.de",
        help="Kein API-Key. Die SEC verlangt bei automatisierten Abrufen einen identifizierbaren User-Agent mit Kontakt.",
    )

    st.divider()
    st.subheader("Automatische Aktualisierung")
    auto_refresh = st.button("🔄 Alles intelligent aktualisieren", type="primary", width="stretch")
    st.caption(
        "Ein Klick prüft zuerst, was bereits aktuell ist. Nur fehlende oder veraltete "
        "Daten werden geladen: Universum → Kurse/Trend → SEC → Finnhub. "
        "Provider-Limits werden automatisch berücksichtigt."
    )

    if AGENT_STATUS_PATH.exists():
        try:
            agent_status = json.loads(AGENT_STATUS_PATH.read_text(encoding="utf-8"))
            finished = str(agent_status.get("finished_at", ""))[:16].replace("T", " ")
            cov = (agent_status.get("coverage") or {}).get("coverage_pct")
            if finished:
                suffix = f" · Ranking-Abdeckung {cov}%" if cov is not None else ""
                st.caption(f"🤖 Hintergrund-Agent zuletzt: {finished} UTC{suffix}")
        except Exception:
            pass

    with st.expander("Erweiterte manuelle Aktualisierung"):
        refresh_universe = st.button("Weltuniversum erzwingen", width="stretch")
        price_scope = st.selectbox("Kursuniversum", ["Top 250", "Top 500", "Top 1000", "Global (~2.200)"], index=1)
        refresh_prices = st.button("Kurs & Trend erzwingen", width="stretch")
        refresh_sec = st.button("SEC-US-Fundamentals erzwingen", width="stretch")
        enrich_batch = st.selectbox("Finnhub-Aktien pro Lauf", [50, 100, 250, 500], index=1)
        enrich_fundamentals = st.button("Finnhub-Warteschlange fortsetzen", width="stretch")

    st.caption(
        "Der Cache bleibt bei normalen App-Neuladungen erhalten. Version 3.3 kann zusätzlich "
        "einen GitHub-Actions-Datenagenten nutzen, der den dauerhaften data_cache automatisch pflegt."
    )

# ---------------------------------------------------------------------------
# Load / refresh live datasets
# ---------------------------------------------------------------------------
data_updated = False
operation_messages: list[tuple[str, str]] = []

if mode == "Demo":
    raw = make_demo_universe()
    universe = raw.copy()
    sec_cache = finnhub_cache = yahoo_cache = price_cache = pd.DataFrame()
    data_label = "Synthetischer Demo-Datensatz"
else:
    universe, sec_cache, finnhub_cache, yahoo_cache, price_cache = load_live_caches()

    if auto_refresh:
        progress_bar = st.progress(0.0, text="Intelligente Aktualisierung wird vorbereitet …")
        def _progress(value: float, stage: str, detail: str) -> None:
            progress_bar.progress(value, text=f"{stage}: {detail}")
        try:
            result = run_refresh_cycle(
                PATHS,
                finnhub_key=finnhub_key,
                sec_user_agent=sec_user_agent,
                policy=RefreshPolicy(
                    price_limit=500,
                    finnhub_limit=200,
                    finnhub_batch=50,
                    max_runtime_seconds=300,
                ),
                progress_cb=_progress,
            )
            universe = result["universe"]
            sec_cache = result["sec"]
            finnhub_cache = result["finnhub"]
            yahoo_cache = result["yahoo"]
            price_cache = result["prices"]
            operation_messages.extend(result["messages"])
            counters = result.get("counters", {})
            data_updated = any(int(v or 0) > 0 for v in counters.values())
            progress_bar.progress(1.0, text=f"Fertig in {result.get('elapsed_seconds', 0)} Sekunden.")
        except Exception as exc:
            progress_bar.empty()
            operation_messages.append(("error", f"Automatische Aktualisierung: {exc}"))

    # First-ever start still obtains the universe automatically, so the app is usable
    # before the user presses the one-click updater.
    if universe.empty:
        try:
            with st.spinner("Offizielles iShares-ACWI-Universum wird einmalig geladen …"):
                universe = fetch_acwi_universe()
                save_universe(universe, PATHS["universe"])
            operation_messages.append(("success", f"Universum initialisiert: {len(universe):,} Aktien.".replace(",", ".")))
            data_updated = True
        except Exception as exc:
            st.error(str(exc))
            st.info("Falls der Abruf temporär blockiert ist, später erneut versuchen oder den Demo-Modus verwenden.")
            st.stop()

    # Optional expert controls. They intentionally remain hidden in an expander.
    if refresh_universe and not auto_refresh:
        try:
            with st.spinner("Weltuniversum wird erzwungen aktualisiert …"):
                universe = fetch_acwi_universe()
                save_universe(universe, PATHS["universe"])
            operation_messages.append(("success", f"Universum aktualisiert: {len(universe):,} Aktien.".replace(",", ".")))
            data_updated = True
        except Exception as exc:
            operation_messages.append(("warning", f"Universum blieb im Cache: {exc}"))

    if refresh_prices and not auto_refresh and not universe.empty:
        try:
            scope = weighted_universe_slice(universe, price_scope)
            with st.spinner(f"Kurs- und Trenddaten für {len(scope):,} Aktien werden erzwungen …".replace(",", ".")):
                fresh_prices = fetch_yahoo_price_snapshot(scope)
                price_cache = upsert_cache(price_cache, fresh_prices)
                save_cache(price_cache, PATHS["prices"])
            useful = int(pd.to_numeric(fresh_prices.get("price", pd.Series(index=fresh_prices.index, dtype=float)), errors="coerce").notna().sum()) if not fresh_prices.empty else 0
            operation_messages.append(("success" if useful else "warning", f"Kursdaten: {useful} erfolgreich aktualisiert."))
            data_updated = True
        except Exception as exc:
            operation_messages.append(("error", f"Yahoo-Chart-Kursabruf: {exc}"))

    if refresh_sec and not auto_refresh and not universe.empty:
        try:
            with st.spinner("SEC-XBRL-Bulkdaten werden erzwungen aktualisiert …"):
                fresh_sec = fetch_sec_bulk_snapshot(universe, sec_user_agent)
                sec_cache = upsert_cache(sec_cache, fresh_sec)
                save_cache(sec_cache, PATHS["sec"])
            operation_messages.append(("success", f"SEC-US-Daten aktualisiert: {len(fresh_sec):,} Aktien.".replace(",", ".")))
            data_updated = True
        except Exception as exc:
            operation_messages.append(("error", f"SEC-Abruf: {exc}"))

    if enrich_fundamentals and not auto_refresh and not universe.empty:
        try:
            if not finnhub_key:
                raise DataSourceError("Für Finnhub bitte den kostenlosen FINNHUB_API_KEY eintragen.")
            symbols = select_next_symbols(universe, finnhub_cache, int(enrich_batch), non_us_first=True, refresh_after_days=14)
            with st.spinner(f"Finnhub arbeitet {len(symbols)} fällige Aktien ab …"):
                fresh = enrich_finnhub_fundamentals(universe, symbols, finnhub_key)
            finnhub_cache = upsert_cache(finnhub_cache, fresh)
            save_cache(finnhub_cache, PATHS["finnhub"])
            stats = provider_batch_stats(fresh)
            kind = "success" if stats["useful"] > 0 else "warning"
            operation_messages.append((kind,
                f"Finnhub: {stats['responses']} Antworten · {stats['useful']} nutzbar · "
                f"{stats['errors']} Fehler · {stats['rate_limits']} Rate-Limit."
            ))
            data_updated = True
        except Exception as exc:
            operation_messages.append(("error", f"Fundamental-Ergänzung: {exc}"))

    raw = assemble_snapshot(universe, sec_cache, finnhub_cache, yahoo_cache, price_cache)
    data_label = "Kostenlose Multi-Source-Daten"

for kind, msg in operation_messages:
    getattr(st, kind)(msg)

# ---------------------------------------------------------------------------
# Score + history + research signals
# ---------------------------------------------------------------------------
history = load_history(PATHS["history"])
scored_all = add_scores(raw)

# Only sufficiently documented rows receive a global history rank. This prevents
# sparse rows from looking artificially competitive.
history_eligible = (
    pd.to_numeric(scored_all.get("data_completeness"), errors="coerce").fillna(0) >= 55
) & (
    pd.to_numeric(scored_all.get("data_confidence"), errors="coerce").fillna(0) >= 55
)
scored_all["history_rank"] = np.nan
eligible_idx = scored_all.loc[history_eligible].sort_values(["ranking_score", "score_total", "data_confidence"], ascending=False).index
scored_all.loc[eligible_idx, "history_rank"] = np.arange(1, len(eligible_idx) + 1)

scored_all = add_history_deltas(scored_all, history)
scored_all = add_research_signals(scored_all)
scored_all["category"] = [
    category_from_score(s, c, conf)
    for s, c, conf in zip(scored_all["score_total"], scored_all["data_completeness"], scored_all.get("data_confidence", pd.Series(np.nan, index=scored_all.index)))
]

if mode == "Live kostenlos" and data_updated and not scored_all.empty:
    try:
        # History stores the most investable rows, not sparse raw rows.
        hist_source = scored_all.loc[history_eligible].sort_values(["ranking_score", "score_total", "data_confidence"], ascending=False)
        history = append_snapshot(hist_source, PATHS["history"], top_n=HISTORY_TOP_N, keep_days=HISTORY_KEEP_DAYS)
    except Exception as exc:
        st.warning(f"Verlauf konnte nicht gespeichert werden: {exc}")

# UI filters.
scored = scored_all.copy()
scored = scored[
    (pd.to_numeric(scored["data_completeness"], errors="coerce").fillna(0) >= min_completeness)
    & (pd.to_numeric(scored.get("data_confidence"), errors="coerce").fillna(0) >= min_confidence)
]
if exclude_special and "sector" in scored.columns:
    scored = scored[~scored["sector"].astype(str).isin(SPECIAL_SECTORS)]

scored = scored.sort_values(["ranking_score", "score_total", "data_confidence", "score_valuation"], ascending=False).reset_index(drop=True)
scored["rank"] = np.arange(1, len(scored) + 1)
top100 = scored.head(TOP_N).copy()

coverage = coverage_summary(
    scored_all,
    min_completeness=min_completeness,
    min_confidence=min_confidence,
) if mode != "Demo" else {
    "universe": len(raw), "eligible": len(raw), "coverage_pct": 100.0,
    "non_us_coverage_pct": 100.0, "status": "Demo",
    "min_completeness": min_completeness, "min_confidence": min_confidence,
}

# ---------------------------------------------------------------------------
# KPI header
# ---------------------------------------------------------------------------
k1, k2, k3, k4 = st.columns(4)
k1.metric("Universum", f"{int(coverage.get('universe', 0)):,}".replace(",", "."))
k2.metric("Ranking-Abdeckung", f"{float(coverage.get('coverage_pct', 0)):.0f}%")
k3.metric("Nicht-US-Abdeckung", f"{float(coverage.get('non_us_coverage_pct', 0)):.0f}%")
k4.metric("Rankingstatus", str(coverage.get("status", "—")))

if mode != "Demo" and str(coverage.get("status")) == "Vorläufig":
    st.warning(
        "Die globale Ranking-Abdeckung ist noch niedrig. Die aktuelle Top-100-Liste ist **vorläufig** und kann US-/bereits angereicherte Aktien bevorzugen. "
        "Nutze SEC + mehrere Finnhub-Ergänzungsläufe, bis der Rankingstatus belastbarer wird. "
        f"Aktive Schwellen: Vollständigkeit ≥{min_completeness}, Confidence ≥{min_confidence}."
    )

st.markdown(
    f'<div class="small-note">Version {APP_VERSION} · {data_label} · '
    f'{history["snapshot_date"].nunique() if not history.empty and "snapshot_date" in history.columns else 0} Historientage</div>',
    unsafe_allow_html=True,
)

rank_tab, detail_tab, quality_tab, history_tab, method_tab, backup_tab = st.tabs(
    ["🏆 Top 100", "🔎 Aktie", "🧪 Datenqualität", "🕘 Historie", "🧭 Methodik", "💾 Backup"]
)

# ---------------------------------------------------------------------------
# Top 100
# ---------------------------------------------------------------------------
with rank_tab:
    st.subheader("Weltweite Top 100 nach aktuellem Research-Modell")
    if mode == "Demo":
        st.warning("Demo-Modus: Firmen und Kennzahlen sind synthetisch.")

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
    trend_options = sorted(filtered_top.get("trend", pd.Series(dtype=str)).dropna().unique().tolist())
    focus_options = sorted(filtered_top.get("research_focus", pd.Series(dtype=str)).dropna().unique().tolist())
    trend_filter = filter_cols[0].multiselect("Trend", trend_options, placeholder="Alle")
    focus_filter = filter_cols[1].multiselect("Research-Fokus", focus_options, placeholder="Alle")
    if trend_filter:
        filtered_top = filtered_top[filtered_top["trend"].isin(trend_filter)]
    if focus_filter:
        filtered_top = filtered_top[filtered_top["research_focus"].isin(focus_filter)]

    if filtered_top.empty:
        st.info("Mit den aktuellen Filtern gibt es noch keine ausreichend vollständigen Kandidaten.")
    elif view == "Tabelle":
        display = pd.DataFrame({
            "Rang": filtered_top["rank"],
            "Ticker": filtered_top["symbol"],
            "Unternehmen": safe_series(filtered_top, "name").fillna(filtered_top["symbol"]),
            "Land": safe_series(filtered_top, "country").fillna(""),
            "Score": filtered_top["score_total"].round(1),
            "Ranking-Score": filtered_top["ranking_score"].round(1),
            "Confidence": pd.to_numeric(filtered_top.get("data_confidence"), errors="coerce").round(0),
            "Trend": filtered_top["trend"],
            "Score Δ": filtered_top["score_delta"].round(1),
            "Rang Δ": filtered_top["rank_delta"].round(0),
            "Einstieg-Setup": filtered_top["entry_setup_score"].round(0),
            "These-Risiko": filtered_top["thesis_risk_score"].round(0),
            "Exit-Watch": filtered_top["exit_watch"],
            "Research-Fokus": filtered_top["research_focus"],
            "Bewertung": filtered_top["valuation_band"],
            "ROIC": safe_series(filtered_top, "returnOnInvestedCapitalTTM").map(pct),
            "FCF Yield": safe_series(filtered_top, "freeCashFlowYieldTTM").map(pct),
            "DCF MoS": safe_series(filtered_top, "dcf_margin_of_safety").map(pct),
            "EPS-Wachstum": safe_series(filtered_top, "epsGrowth").map(pct),
            "Net Debt/EBITDA": safe_series(filtered_top, "netDebtToEBITDATTM").map(multiple),
            "KGV": safe_series(filtered_top, "priceToEarningsRatioTTM").map(multiple),
            "Quellen": safe_series(filtered_top, "fundamental_sources").fillna(""),
        })
        st.dataframe(display, hide_index=True, width="stretch", height=680)
    else:
        page_size = 20
        pages = max(1, int(np.ceil(len(filtered_top) / page_size)))
        page_no = st.number_input("Seite", min_value=1, max_value=pages, value=1, step=1) if pages > 1 else 1
        page_df = filtered_top.iloc[(int(page_no)-1)*page_size:int(page_no)*page_size]
        for _, row in page_df.iterrows():
            with st.container(border=True):
                left, right = st.columns([3, 1])
                left.markdown(f"**#{int(row['rank'])} · {row['symbol']} · {row.get('name', row['symbol'])}**")
                left.caption(
                    f"{row.get('country','')} · {row['trend']} · {row['research_focus']} · "
                    f"Bewertung: {row['valuation_band']} · Quellen: {row.get('fundamental_sources','—')}"
                )
                right.metric("Ranking", f"{row['ranking_score']:.1f}", delta=signed(row.get("score_delta")))
                a, b, c = st.columns(3)
                a.metric("Einstieg", f"{row['entry_setup_score']:.0f}/100")
                b.metric("These-Risiko", f"{row['thesis_risk_score']:.0f}/100")
                c.metric("Confidence", f"{row.get('data_confidence', np.nan):.0f}%")
                st.caption(
                    f"ROIC {pct(row.get('returnOnInvestedCapitalTTM'))} · FCF Yield {pct(row.get('freeCashFlowYieldTTM'))} · "
                    f"DCF MoS {pct(row.get('dcf_margin_of_safety'))} · EPS {pct(row.get('epsGrowth'))} · "
                    f"KGV {multiple(row.get('priceToEarningsRatioTTM'))} · Exit-Watch: {row['exit_watch']}"
                )

    st.download_button(
        "Top 100 als CSV sichern",
        data=top100.to_csv(index=False).encode("utf-8"),
        file_name=f"global_stock_top100_{datetime.now().date().isoformat()}.csv",
        mime="text/csv",
        width="stretch",
    )

# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------
with detail_tab:
    if scored.empty:
        st.info("Noch keine Aktie erfüllt die Datenfilter. Datenquellen weiter ergänzen oder Schwellen vorübergehend senken.")
    else:
        labels = {
            f"#{int(r['rank'])} · {r['symbol']} · {r.get('name', r['symbol'])}": i
            for i, r in scored.head(max(TOP_N, 300)).iterrows()
        }
        label = st.selectbox("Aktie auswählen", list(labels.keys()))
        row = scored.loc[labels[label]]
        currency = str(row.get("price_currency", row.get("currency", "")) or "")

        st.subheader(f"{row.get('name', row['symbol'])} ({row['symbol']})")
        st.caption(
            f"{row.get('country','')} · {row.get('sector','')} · Quellen {row.get('fundamental_sources','—')} · "
            f"Data Confidence {row.get('data_confidence', np.nan):.0f}%"
        )

        a, b, c, d = st.columns(4)
        a.metric("Gesamtscore", f"{row['score_total']:.1f}/100", delta=signed(row.get("score_delta")))
        b.metric("Einstieg-Setup", f"{row['entry_setup_score']:.0f}/100")
        c.metric("These-Risiko", f"{row['thesis_risk_score']:.0f}/100")
        d.metric("Rang", f"#{int(row['rank'])}", delta=signed(row.get("rank_delta")))
        st.caption(f"Confidence-adjustierter Ranking-Score: **{row.get('ranking_score', np.nan):.1f}/100**. Der Fundamentalscore bleibt davon getrennt sichtbar.")

        st.markdown("#### Qualität & Wachstum")
        a, b, c, d = st.columns(4)
        a.metric("ROIC", pct(row.get("returnOnInvestedCapitalTTM")))
        b.metric("Operative Marge", pct(row.get("operatingProfitMarginTTM")))
        c.metric("Umsatzwachstum", pct(row.get("revenueGrowth")))
        d.metric("EPS-Wachstum", pct(row.get("epsGrowth")))
        e, f, g, h = st.columns(4)
        e.metric("FCF-Marge", pct(row.get("fcfMarginTTM")))
        f.metric("FCF-Wachstum", pct(row.get("fcfGrowth")))
        g.metric("FCF Yield", pct(row.get("freeCashFlowYieldTTM")))
        h.metric("Verwässerung", pct(row.get("sharesGrowth")))

        st.markdown("#### Bewertung & Reverse-DCF-Proxy")
        a, b, c, d = st.columns(4)
        a.metric("KGV", multiple(row.get("priceToEarningsRatioTTM")))
        b.metric("EV / EBITDA", multiple(row.get("enterpriseValueMultipleTTM")))
        c.metric("DCF Margin of Safety", pct(row.get("dcf_margin_of_safety")))
        d.metric("Implizites FCF-Wachstum 10J", pct(row.get("implied_fcf_growth_10y")))
        z = row.get("valuation_zscore", np.nan)
        if pd.notna(z):
            st.caption(
                f"Historischer Bewertungs-Kontext: P/E-Z-Score {float(z):+.2f}; "
                f"5J-Median {multiple(row.get('pe_5y_median'))}. Negativer Z-Score = unter eigener Historie."
            )
        st.caption(
            "DCF-Proxy: heutiger FCF Yield als Ausgangspunkt, 10 Jahre, 10% Diskontsatz und 2,5% Terminalwachstum. "
            "Das ist ein Szenario-Tool, kein objektiver fairer Wert."
        )

        st.markdown("#### Bilanz & Kursstruktur")
        a, b, c, d = st.columns(4)
        a.metric("Net Debt / EBITDA", multiple(row.get("netDebtToEBITDATTM")))
        b.metric("Current Ratio", multiple(row.get("currentRatioTTM")))
        c.metric("Kurs", price(row.get("price"), currency))
        d.metric("52W Hoch Abstand", pct(row.get("distance_52w_high")))
        e, f, g, h = st.columns(4)
        e.metric("50-Tage-Linie", price(row.get("priceAvg50"), currency))
        f.metric("200-Tage-Linie", price(row.get("priceAvg200"), currency))
        g.metric("6M", pct(row.get("return6m")))
        h.metric("12M", pct(row.get("return12m")))
        st.caption(f"Technischer Kontext: **{row['technical_trend']}**. Fundamentaldaten haben im Modell Vorrang.")

        st.markdown("#### Einstieg & Exit-Watch")
        left, right = st.columns(2)
        with left:
            if row.get("research_focus") == "Einstieg analysieren":
                st.success(f"Research-Fokus: **{row['research_focus']}**")
            elif "Daten" in str(row.get("research_focus")):
                st.info(f"Research-Fokus: **{row['research_focus']}**")
            else:
                st.warning(f"Research-Fokus: **{row['research_focus']}**")
        with right:
            if row.get("exit_watch") == "Keine starke Warnung":
                st.success(f"Exit-Watch: **{row['exit_watch']}**")
            else:
                st.warning(f"Exit-Watch: **{row['exit_watch']}**")
        st.caption(f"These-Monitor: {row.get('thesis_flags', '—')}")

        st.markdown("#### Score-Aufteilung")
        score_table = pd.DataFrame({
            "Bereich": ["Qualität", "Wachstum", "Cashflow", "Bilanz", "Kapitalallokation", "Moat-Proxy", "Bewertung", "Risiko"],
            "Punkte": [row["score_quality"], row["score_growth"], row["score_cashflow"], row["score_balance"], row["score_capital_allocation"], row["score_moat"], row["score_valuation"], row["score_risk"]],
            "Maximum": [20, 15, 15, 10, 10, 10, 15, 5],
        })
        score_table["Erfüllung %"] = score_table["Punkte"] / score_table["Maximum"] * 100
        st.bar_chart(score_table.set_index("Bereich")["Erfüllung %"], horizontal=True)

        st.markdown("#### Datenherkunft")
        prov = provenance_table(row)
        prov["Wert"] = pd.to_numeric(prov["Wert"], errors="coerce")
        st.dataframe(prov, hide_index=True, width="stretch")

        # Optional analyst context from Yahoo; never part of the fundamental score.
        if pd.notna(row.get("targetMeanPrice")) or pd.notna(row.get("analystCount")):
            st.markdown("#### Analystenkontext (optional, nicht im Hauptscore)")
            a, b, c = st.columns(3)
            a.metric("Mittleres Kursziel", price(row.get("targetMeanPrice"), currency))
            b.metric("Analysten", f"{row.get('analystCount', np.nan):.0f}" if pd.notna(row.get("analystCount")) else "—")
            c.metric("Recommendation Mean", f"{row.get('recommendationMean', np.nan):.2f}" if pd.notna(row.get("recommendationMean")) else "—")

        if mode == "Live kostenlos":
            st.caption(
                "Yahoo-QuoteSummary/Statements werden in Version 3.3 nicht live abgerufen, "
                "weil Streamlit-Cloud-IP-Adressen häufig 401/Invalid-Crumb erhalten. "
                "Bereits gecachte Yahoo-Werte bleiben sichtbar, fließen aber nur als Fallback ein."
            )

# ---------------------------------------------------------------------------
# Data quality / diagnostics
# ---------------------------------------------------------------------------
with quality_tab:
    st.subheader("Abdeckung & Datenvertrauen")
    q1, q2, q3, q4 = st.columns(4)
    q1.metric("Rankingstatus", str(coverage.get("status", "—")))
    q2.metric("Rankingfähig", f"{int(coverage.get('eligible', 0)):,}".replace(",", "."))
    q3.metric("Gesamt-Abdeckung", f"{float(coverage.get('coverage_pct', 0)):.1f}%")
    q4.metric("Nicht-US-Abdeckung", f"{float(coverage.get('non_us_coverage_pct', 0)):.1f}%")

    if mode != "Demo":
        provider_rows = [
            {
                "Quelle": "iShares ACWI", "Cache-Zeilen": len(universe), "Nutzbar": len(universe),
                "Fehler": 0, "Rate-Limit": 0, "Letzte Aktualisierung": "—",
                "Rolle": "Weltweites Aktienuniversum",
            },
            {**provider_diagnostics(sec_cache, "SEC EDGAR"), "Rolle": "Offizielle US-XBRL-Fundamentals"},
            {**provider_diagnostics(finnhub_cache, "Finnhub"), "Rolle": "Globale Basic Financials / Ratios"},
            {**provider_diagnostics(yahoo_cache, "Yahoo Fundamentals (Alt-Cache)"), "Rolle": "Nur vorhandener Cache; neue Live-Abrufe in 3.3 deaktiviert"},
            {
                "Quelle": "Yahoo Chart Kurse",
                "Cache-Zeilen": len(price_cache),
                "Nutzbar": int(pd.to_numeric(price_cache.get("price", pd.Series(index=price_cache.index, dtype=float)), errors="coerce").notna().sum()) if not price_cache.empty else 0,
                "Fehler": int(price_cache.get("provider_error", pd.Series(index=price_cache.index, dtype=object)).fillna("").astype(str).ne("").sum()) if not price_cache.empty else 0,
                "Rate-Limit": int(price_cache.get("provider_error", pd.Series(index=price_cache.index, dtype=object)).fillna("").astype(str).eq("RATE_LIMIT").sum()) if not price_cache.empty else 0,
                "Letzte Aktualisierung": "—",
                "Rolle": "Crumb-freier v8-Chart: Kurs, 50/200T, 52W, Momentum",
            },
        ]
        providers = pd.DataFrame(provider_rows)
        st.dataframe(providers, hide_index=True, width="stretch")
        st.caption(
            "Nutzbar = mindestens 5 Kern-Fundamentalkennzahlen im jeweiligen Provider-Cache. "
            "Fehlgeschlagene Finnhub-Fundamentalabrufe werden 7 Tage nicht erneut ausgewählt. Yahoo-Fundamentals sind live deaktiviert."
        )

    if not raw.empty and "country" in raw.columns:
        tmp = scored_all.copy()
        tmp["_confidence"] = pd.to_numeric(tmp.get("data_confidence"), errors="coerce").fillna(0)
        tmp["_completeness"] = pd.to_numeric(tmp.get("data_completeness"), errors="coerce").fillna(0)
        tmp["_eligible"] = (tmp["_confidence"] >= min_confidence) & (tmp["_completeness"] >= min_completeness)
        country_cov = tmp.groupby("country", dropna=False).agg(
            Aktien=("symbol", "count"),
            Ausreichend=("_eligible", "sum"),
            Median_Confidence=("_confidence", "median"),
        ).reset_index()
        country_cov["Ausreichend"] = country_cov["Ausreichend"].astype(int)
        country_cov["Abdeckung %"] = (country_cov["Ausreichend"] / country_cov["Aktien"] * 100).round(1)
        country_cov = country_cov.sort_values("Aktien", ascending=False).head(30)
        st.markdown("#### Abdeckung nach Land")
        st.dataframe(country_cov, hide_index=True, width="stretch")

    st.info(
        "Data Confidence bewertet **Vollständigkeit + Quellenqualität + Kursverfügbarkeit**. "
        "Eine hohe Kennzahl macht die Daten nicht fehlerfrei, verhindert aber, dass sehr unvollständige Aktien im Ranking zu viel Gewicht bekommen."
    )

# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
with history_tab:
    st.subheader("Veränderungshistorie")
    if history.empty:
        st.info("Noch kein historischer Snapshot vorhanden. Nach Datenaktualisierungen wird täglich ein Ranking-Snapshot gespeichert.")
    else:
        hist_symbols = sorted(history["symbol"].dropna().astype(str).unique().tolist())
        default_symbol = str(top100.iloc[0]["symbol"]) if not top100.empty and str(top100.iloc[0]["symbol"]) in hist_symbols else hist_symbols[0]
        symbol = st.selectbox("Ticker", hist_symbols, index=hist_symbols.index(default_symbol))
        sh = symbol_history(history, symbol)
        if not sh.empty:
            chart_cols = [c for c in ["score_total", "score_quality", "score_growth", "score_cashflow", "score_valuation"] if c in sh.columns]
            chart = sh.set_index("snapshot_date")[chart_cols].apply(pd.to_numeric, errors="coerce")
            st.line_chart(chart)
            if "history_rank" in sh.columns:
                st.markdown("#### Rangverlauf")
                rank_chart = sh.set_index("snapshot_date")[["history_rank"]].apply(pd.to_numeric, errors="coerce")
                st.line_chart(rank_chart)
            st.dataframe(sh.tail(30), hide_index=True, width="stretch")
        st.download_button(
            "Historie als CSV sichern",
            data=history.to_csv(index=False).encode("utf-8"),
            file_name="ranking_history.csv",
            mime="text/csv",
            width="stretch",
        )

# ---------------------------------------------------------------------------
# Methodology
# ---------------------------------------------------------------------------
with method_tab:
    st.subheader("Methodik")
    st.markdown(
        """
**Datenarchitektur.** Das Universum stammt aus den offiziellen Positionen des iShares MSCI ACWI ETF. Für US-Unternehmen nutzt die App kostenlose SEC-EDGAR-XBRL-Daten. Internationale Kennzahlen werden schrittweise über Finnhub Basic Financials ergänzt. Yahoo dient in 3.3 nur noch über den crumb-freien v8-Chart-Endpunkt für Kurs- und Trenddaten; QuoteSummary-Fundamentals sind live deaktiviert.

**100-Punkte-Modell.** Unternehmensqualität 20, Wachstum 15, Free Cashflow 15, Bilanz 10, Kapitalallokation 10, Moat-Proxy 10, Bewertung 15 und Risiko 5 Punkte. Harte Red Flags ziehen Punkte ab. Moat und Management werden ausdrücklich nur über quantitative Proxies angenähert.

**Einstieg-Setup.** Qualität, Cashflow und Bewertung werden separat kombiniert. Der DCF-Proxy kann den Einstiegsscore nur leicht nach oben oder unten verschieben. Ein guter Kurs-Chart kann eine fundamentale Verschlechterung nicht überstimmen.

**Exit-Watch.** Das System reagiert auf schrumpfenden FCF/EPS/Umsatz, niedrigen ROIC, hohe Verschuldung, Verwässerung, Score-Verfall, anspruchsvolle DCF-Annahmen und die Kombination aus fundamentalem und technischem Abwärtstrend. Es erzeugt bewusst Prüfhinweise statt automatischer Verkaufsorders.

**Reverse DCF.** Aus dem aktuellen FCF Yield wird berechnet, welches langfristige FCF-Wachstum ungefähr erforderlich wäre, um den aktuellen Preis unter den Modellannahmen zu rechtfertigen. Niedrigere implizite Erwartungen sind grundsätzlich leichter zu erfüllen als sehr hohe.

**Rankingstatus.** Solange ein großer Teil der internationalen Aktien noch keine ausreichende Fundamentalabdeckung besitzt, wird das globale Ranking als *vorläufig* markiert. Das verhindert falsche Präzision.
        """
    )

# ---------------------------------------------------------------------------
# Backup / restore
# ---------------------------------------------------------------------------
with backup_tab:
    st.subheader("Backup für Streamlit Cloud")
    st.caption(
        "Community-Cloud kann lokalen Speicher bei Neustarts zurücksetzen. Sichere deshalb gelegentlich die komplette Datenbasis als ZIP und spiele sie bei Bedarf wieder ein."
    )
    backup_bytes = build_backup_zip()
    st.download_button(
        "Komplettes Daten-Backup herunterladen",
        data=backup_bytes,
        file_name=f"global_stock_ranker_backup_{datetime.now().date().isoformat()}.zip",
        mime="application/zip",
        width="stretch",
    )

    upload = st.file_uploader("Backup-ZIP wiederherstellen", type=["zip"])
    if upload is not None and st.button("Backup wiederherstellen", width="stretch"):
        try:
            restored = restore_backup(upload)
            st.success("Wiederhergestellt: " + ", ".join(restored))
            st.rerun()
        except Exception as exc:
            st.error(f"Backup konnte nicht wiederhergestellt werden: {exc}")

    st.markdown("#### Alternative: einzelne Historie importieren")
    history_upload = st.file_uploader("Historie CSV / CSV.GZ", type=["csv", "gz"], key="history_import")
    if history_upload is not None and st.button("Historie zusammenführen", width="stretch"):
        try:
            imported = import_cache_bytes(history_upload)
            merged = merge_imported_history(load_history(PATHS["history"]), imported)
            merged.to_csv(PATHS["history"], index=False, compression="gzip")
            st.success(f"{len(imported)} Historienzeilen importiert.")
            st.rerun()
        except Exception as exc:
            st.error(f"Historie konnte nicht importiert werden: {exc}")
