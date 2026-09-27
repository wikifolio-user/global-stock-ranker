from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

import pandas as pd

from data_sources import (
    DataSourceError,
    enrich_finnhub_fundamentals,
    fetch_sec_bulk_snapshot,
    fetch_yahoo_price_snapshot,
)
from pipeline import (
    load_cache,
    provider_batch_stats,
    save_cache,
    select_next_symbols,
    select_stale_symbols,
    upsert_cache,
)
from settings import (
    FINNHUB_REFRESH_DAYS,
    PRICE_FAILURE_COOLDOWN_HOURS,
    PRICE_REFRESH_HOURS,
    SEC_CACHE_DAYS,
    UNIVERSE_CACHE_HOURS,
)
from universe import fetch_acwi_universe, load_universe_cache, save_universe


ProgressCallback = Callable[[float, str, str], None]


@dataclass(frozen=True)
class RefreshPolicy:
    price_limit: int = 250
    finnhub_limit: int = 150
    finnhub_batch: int = 50
    max_runtime_seconds: int = 240
    refresh_universe: bool = True
    refresh_prices: bool = True
    refresh_sec: bool = True
    refresh_finnhub: bool = True


def _now_utc() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


def _latest_timestamp(frame: pd.DataFrame, column: str) -> pd.Timestamp | None:
    if frame is None or frame.empty or column not in frame.columns:
        return None
    s = pd.to_datetime(frame[column], errors="coerce", utc=True).dropna()
    return s.max() if not s.empty else None


def frame_is_stale(frame: pd.DataFrame, column: str, max_age_hours: float) -> bool:
    latest = _latest_timestamp(frame, column)
    if latest is None:
        return True
    return latest < (_now_utc() - pd.Timedelta(hours=float(max_age_hours)))


def _emit(cb: ProgressCallback | None, progress: float, stage: str, detail: str) -> None:
    if cb:
        cb(max(0.0, min(1.0, float(progress))), stage, detail)


def run_refresh_cycle(
    paths: Mapping[str, Path],
    finnhub_key: str,
    sec_user_agent: str,
    policy: RefreshPolicy,
    progress_cb: ProgressCallback | None = None,
) -> dict[str, object]:
    """Run a smart refresh cycle that only requests missing or stale data.

    The function is shared by the Streamlit one-button updater and the scheduled
    GitHub Actions data agent. It preserves cached values and stops gracefully on
    provider limits or runtime budget exhaustion.
    """
    started = time.monotonic()
    messages: list[tuple[str, str]] = []
    counters = {
        "universe_updated": 0,
        "prices_attempted": 0,
        "prices_useful": 0,
        "sec_rows": 0,
        "finnhub_attempted": 0,
        "finnhub_useful": 0,
        "rate_limited": 0,
    }

    universe = load_universe_cache(paths["universe"])
    sec_cache = load_cache(paths["sec"])
    finnhub_cache = load_cache(paths["finnhub"])
    yahoo_cache = load_cache(paths["yahoo"])
    price_cache = load_cache(paths["prices"])

    # 1) Universe: only if missing or stale.
    _emit(progress_cb, 0.03, "Universum", "Prüfe, ob das Aktienuniversum aktuell ist …")
    universe_stale = frame_is_stale(universe, "universe_updated_at", UNIVERSE_CACHE_HOURS)
    if policy.refresh_universe and (universe.empty or universe_stale):
        try:
            fresh_universe = fetch_acwi_universe()
            if not fresh_universe.empty:
                universe = fresh_universe
                save_universe(universe, paths["universe"])
                counters["universe_updated"] = len(universe)
                messages.append(("success", f"Universum aktualisiert: {len(universe):,} Aktien.".replace(",", ".")))
        except Exception as exc:
            if universe.empty:
                raise
            messages.append(("warning", f"Universum blieb im Cache: {exc}"))
    else:
        messages.append(("info", "Universum ist noch aktuell – kein erneuter Download nötig."))

    if universe.empty:
        raise DataSourceError("Kein Aktienuniversum verfügbar.")

    # 2) Prices/trend: refresh only stale/missing rows and respect recent failures.
    _emit(progress_cb, 0.15, "Kurse & Trend", "Ermittle fehlende oder veraltete Kursdaten …")
    if policy.refresh_prices and policy.price_limit > 0:
        price_symbols = select_stale_symbols(
            universe,
            price_cache,
            timestamp_col="price_updated_at",
            max_age_hours=PRICE_REFRESH_HOURS,
            limit=int(policy.price_limit),
            failure_cooldown_hours=PRICE_FAILURE_COOLDOWN_HOURS,
            non_us_first=False,
        )
        if price_symbols:
            fresh_prices = fetch_yahoo_price_snapshot(universe, symbols=price_symbols)
            counters["prices_attempted"] = len(fresh_prices)
            if not fresh_prices.empty:
                useful = pd.to_numeric(fresh_prices.get("price", pd.Series(index=fresh_prices.index, dtype=float)), errors="coerce").notna()
                counters["prices_useful"] = int(useful.sum())
                if fresh_prices.get("provider_error", pd.Series(dtype=object)).astype(str).eq("RATE_LIMIT").any():
                    counters["rate_limited"] += 1
                price_cache = upsert_cache(price_cache, fresh_prices)
                save_cache(price_cache, paths["prices"])
            messages.append((
                "success" if counters["prices_useful"] else "warning",
                f"Kurse: {len(price_symbols)} fällig · {counters['prices_useful']} erfolgreich. "
                "Bereits aktuelle Kurse wurden übersprungen.",
            ))
        else:
            messages.append(("info", "Kurse & Trend sind innerhalb des Aktualisierungsfensters bereits aktuell."))

    # 3) SEC: bulk refresh only once per cache window.
    _emit(progress_cb, 0.48, "SEC Fundamentals", "Prüfe offizielle US-Fundamentaldaten …")
    sec_stale = frame_is_stale(sec_cache, "fundamental_updated_at", SEC_CACHE_DAYS * 24)
    if policy.refresh_sec and sec_stale:
        if sec_user_agent:
            try:
                fresh_sec = fetch_sec_bulk_snapshot(universe, sec_user_agent)
                counters["sec_rows"] = len(fresh_sec)
                if not fresh_sec.empty:
                    sec_cache = upsert_cache(sec_cache, fresh_sec)
                    save_cache(sec_cache, paths["sec"])
                messages.append(("success", f"SEC: {len(fresh_sec):,} US-Aktien aktualisiert.".replace(",", ".")))
            except Exception as exc:
                messages.append(("warning", f"SEC blieb im Cache: {exc}"))
        else:
            messages.append(("warning", "SEC_USER_AGENT fehlt – vorhandene SEC-Daten bleiben erhalten."))
    else:
        messages.append(("info", "SEC-Fundamentals sind noch aktuell – kein erneuter Bulk-Abruf nötig."))

    # 4) Finnhub: advance through the missing/stale queue until budget/limit is reached.
    _emit(progress_cb, 0.67, "Internationale Fundamentals", "Arbeite die Finnhub-Warteschlange automatisch ab …")
    if policy.refresh_finnhub and policy.finnhub_limit > 0:
        if not finnhub_key:
            messages.append(("warning", "FINNHUB_API_KEY fehlt – internationale Fundamentals wurden übersprungen."))
        else:
            remaining = int(policy.finnhub_limit)
            while remaining > 0 and (time.monotonic() - started) < policy.max_runtime_seconds:
                batch_n = min(int(policy.finnhub_batch), remaining)
                symbols = select_next_symbols(
                    universe,
                    finnhub_cache,
                    batch_n,
                    non_us_first=True,
                    retry_after_days=7,
                    refresh_after_days=FINNHUB_REFRESH_DAYS,
                )
                if not symbols:
                    break
                fresh = enrich_finnhub_fundamentals(universe, symbols, finnhub_key)
                stats = provider_batch_stats(fresh)
                counters["finnhub_attempted"] += stats["responses"]
                counters["finnhub_useful"] += stats["useful"]
                counters["rate_limited"] += stats["rate_limits"]
                if not fresh.empty:
                    finnhub_cache = upsert_cache(finnhub_cache, fresh)
                    save_cache(finnhub_cache, paths["finnhub"])
                remaining -= max(1, stats["responses"])
                done_fraction = 1.0 - (remaining / max(1, int(policy.finnhub_limit)))
                _emit(
                    progress_cb,
                    0.67 + 0.28 * done_fraction,
                    "Internationale Fundamentals",
                    f"Finnhub: {counters['finnhub_attempted']} geprüft · {counters['finnhub_useful']} nutzbar …",
                )
                if stats["rate_limits"] > 0:
                    messages.append(("warning", "Finnhub-Rate-Limit erreicht. Der nächste Lauf setzt automatisch an dieser Stelle fort."))
                    break
                if stats["responses"] == 0:
                    break
            if counters["finnhub_attempted"]:
                messages.append((
                    "success" if counters["finnhub_useful"] else "warning",
                    f"Finnhub: {counters['finnhub_attempted']} Antworten · {counters['finnhub_useful']} nutzbar. "
                    "Bereits aktuelle/zuletzt fehlgeschlagene Aktien wurden automatisch übersprungen.",
                ))
            else:
                messages.append(("info", "Finnhub-Warteschlange enthält derzeit keine fälligen Aktien."))

    _emit(progress_cb, 1.0, "Fertig", "Aktualisierung abgeschlossen. Ranking wird neu berechnet …")
    return {
        "universe": universe,
        "sec": sec_cache,
        "finnhub": finnhub_cache,
        "yahoo": yahoo_cache,
        "prices": price_cache,
        "messages": messages,
        "counters": counters,
        "elapsed_seconds": round(time.monotonic() - started, 1),
    }
