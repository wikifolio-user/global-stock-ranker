from __future__ import annotations

import shutil
from pathlib import Path
from typing import Mapping


CACHE_FILES = {
    "universe": "universe.csv.gz",
    "sec": "sec_fundamentals.csv.gz",
    "finnhub": "finnhub_fundamentals.csv.gz",
    "yahoo": "yahoo_fundamentals.csv.gz",
    "prices": "yahoo_prices.csv.gz",
    "history": "ranking_history.csv.gz",
}


def persisted_paths(base_dir: str | Path = "data_cache") -> dict[str, Path]:
    base = Path(base_dir)
    return {name: base / filename for name, filename in CACHE_FILES.items()}


def bootstrap_local_cache(local_paths: Mapping[str, Path], persisted_dir: str | Path = "data_cache") -> list[str]:
    """Seed an empty local Streamlit cache from repository-persisted cache files.

    Streamlit Community Cloud local storage can disappear on redeploy/reboot. The
    background GitHub Actions agent stores the latest durable cache under data_cache/.
    On a fresh process we copy those files into the fast local cache. Existing local
    files are never overwritten, so interactive updates from the current session win.
    """
    restored: list[str] = []
    sources = persisted_paths(persisted_dir)
    for name, target in local_paths.items():
        source = sources.get(name)
        if source is None or not source.exists() or target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        restored.append(name)
    return restored
