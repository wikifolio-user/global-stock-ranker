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
    price_limit: int = 500
    finnhub_limit: int = 240
    finnhub_batch: int = 40
    max_runtime_seconds: int = 300
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


def _load_all(paths: Mapping[str, Path]):
    return (
        load_universe_cache(paths["universe"]),
        load_cache(paths["sec"]),
        load_cache(paths["finnhub"]),
        load_cache(paths["yahoo"]),
        load_cache(paths["prices"]),
    )


def refresh_plan(paths: Mapping[str, Path]) -> dict[str, int | bool | str]:
    """Inspect local cache and report what is actually due without network traffic."""
    universe, sec_cache, finnhub_cache, _, price_cache = _load_all(paths)
    if universe.empty:
        return {
            "universe_rows": 0,
            "universe_due": True,
            "prices_due": 0,
            "sec_due": True,
            "finnhub_due": 0,
            "finnhub_cached": int(len(finnhub_cache)),
            "price_cached": int(len(price_cache)),
        }

    price_due = select_stale_symbols(
        universe,
        price_cache,
        timestamp_col="price_updated_at",
        max_age_hours=PRICE_REFRESH_HOURS,
        limit=len(universe),
        failure_cooldown_hours=PRICE_FAILURE_COOLDOWN_HOURS,
        non_us_first=False,
    )
    finn_due = select_next_symbols(
        universe,
        finnhub_cache,
        limit=len(universe),
        non_us_first=True,
        retry_after_days=7,
        refresh_after_days=FINNHUB_REFRESH_DAYS,
    )
    return {
        "universe_rows": int(len(universe)),
        "universe_due": bool(frame_is_stale(universe, "universe_updated_at", UNIVERSE_CACHE_HOURS)),
        "prices_due": int(len(price_due)),
        "sec_due": bool(frame_is_stale(sec_cache, "fundamental_updated_at", SEC_CACHE_DAYS * 24)),
        "finnhub_due": int(len(finn_due)),
        "finnhub_cached": int(len(finnhub_cache)),
        "price_cached": int(len(price_cache)),
    }


def run_refresh_cycle(
    paths: Mapping[str, Path],
    finnhub_key: str,
    sec_user_agent: str,
    policy: RefreshPolicy,
    progress_cb: ProgressCallback | None = None,
) -> dict[str, object]:
    """Run one resumable smart refresh cycle.

    Fresh rows are never downloaded just because the button was pressed again. The
    function advances through queues, stores every completed attempt, respects
    provider cooldowns, and can therefore be called repeatedly or by GitHub Actions
    without starting over.
    """
    started = time.monotonic()
    messages: list[tuple[str, str]] = []
    counters = {
        "universe_updated": 0,
        "prices_requested": 0,
        "prices_responses": 0,
        "prices_useful": 0,
        "sec_rows": 0,
        "finnhub_requested": 0,
        "finnhub_responses": 0,
        "finnhub_useful": 0,
        "finnhub_errors": 0,
        "rate_limited": 0,
    }

    universe, sec_cache, finnhub_cache, yahoo_cache, price_cache = _load_all(paths)

    # 1) Universe
    _emit(progress_cb, 0.02, "Universum", "Prüfe Fälligkeit …")
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
        messages.append(("info", "Universum ist frisch – übersprungen."))

    if universe.empty:
        raise DataSourceError("Kein Aktienuniversum verfügbar.")

    # 2) Prices/trend
    _emit(progress_cb, 0.12, "Kurse & Trend", "Suche nur fehlende/veraltete Kurse …")
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
        counters["prices_requested"] = len(price_symbols)
        if price_symbols:
            fresh_prices = fetch_yahoo_price_snapshot(universe, symbols=price_symbols)
            counters["prices_responses"] = len(fresh_prices)
            if not fresh_prices.empty:
                useful = pd.to_numeric(
                    fresh_prices.get("price", pd.Series(index=fresh_prices.index, dtype=float)),
                    errors="coerce",
                ).notna()
                counters["prices_useful"] = int(useful.sum())
                errors = fresh_prices.get("provider_error", pd.Series("", index=fresh_prices.index)).fillna("").astype(str)
                if errors.eq("RATE_LIMIT").any():
                    counters["rate_limited"] += 1
                price_cache = upsert_cache(price_cache, fresh_prices)
                save_cache(price_cache, paths["prices"])
            messages.append((
                "success" if counters["prices_useful"] else "warning",
                f"Kurse: {len(price_symbols)} fällig · {counters['prices_useful']} erfolgreich. Frische Kurse blieben im Cache.",
            ))
        else:
            messages.append(("info", "Kurse & Trend sind bereits aktuell – 0 Downloads."))

    # 3) SEC
    _emit(progress_cb, 0.43, "SEC Fundamentals", "Prüfe offiziellen US-Cache …")
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
        messages.append(("info", "SEC-Fundamentals sind frisch – übersprungen."))

    # 4) Finnhub queue
    _emit(progress_cb, 0.62, "Internationale Fundamentals", "Setze die offene Warteschlange fort …")
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
                counters["finnhub_requested"] += len(symbols)
                fresh = enrich_finnhub_fundamentals(universe, symbols, finnhub_key)
                stats = provider_batch_stats(fresh)
                counters["finnhub_responses"] += stats["responses"]
                counters["finnhub_useful"] += stats["useful"]
                counters["finnhub_errors"] += stats["errors"]
                counters["rate_limited"] += stats["rate_limits"]
                if not fresh.empty:
                    finnhub_cache = upsert_cache(finnhub_cache, fresh)
                    save_cache(finnhub_cache, paths["finnhub"])

                # Count actual responses, because rate-limited batches can end early.
                consumed = max(1, stats["responses"])
                remaining -= consumed
                done_fraction = 1.0 - (remaining / max(1, int(policy.finnhub_limit)))
                _emit(
                    progress_cb,
                    0.62 + 0.34 * done_fraction,
                    "Internationale Fundamentals",
                    f"{counters['finnhub_responses']} geprüft · {counters['finnhub_useful']} breit nutzbar · {counters['finnhub_errors']} Fehler …",
                )
                if stats["rate_limits"] > 0:
                    messages.append(("warning", "Finnhub-Rate-Limit erreicht. Der nächste Lauf setzt automatisch bei der nächsten fälligen Aktie fort."))
                    break
                if stats["responses"] == 0:
                    break

            if counters["finnhub_responses"]:
                messages.append((
                    "success" if counters["finnhub_useful"] else "info",
                    f"Finnhub: {counters['finnhub_responses']} Antworten · {counters['finnhub_useful']} mit breiter Kennzahlenabdeckung · "
                    f"{counters['finnhub_errors']} Fehler. Auch dünne erfolgreiche Antworten werden jetzt 14 Tage gecacht statt immer neu geladen.",
                ))
            else:
                messages.append(("info", "Finnhub-Warteschlange enthält derzeit keine fälligen Aktien."))

    final_plan = refresh_plan(paths)
    _emit(progress_cb, 1.0, "Fertig", "Cache gespeichert; Ranking wird neu berechnet …")
    return {
        "universe": universe,
        "sec": sec_cache,
        "finnhub": finnhub_cache,
        "yahoo": yahoo_cache,
        "prices": price_cache,
        "messages": messages,
        "counters": counters,
        "remaining": final_plan,
        "elapsed_seconds": round(time.monotonic() - started, 1),
    }
