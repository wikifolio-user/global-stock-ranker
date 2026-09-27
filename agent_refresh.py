from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from history import append_snapshot, load_history
from persistence import persisted_paths
from pipeline import assemble_snapshot, coverage_summary, eligibility_masks
from refresh_engine import RefreshPolicy, refresh_plan, run_refresh_cycle
from scoring import add_scores
from settings import (
    DEFAULT_MIN_COMPLETENESS,
    DEFAULT_MIN_CONFIDENCE,
    GLOBAL_LITE_ENABLED_DEFAULT,
    HISTORY_KEEP_DAYS,
    HISTORY_TOP_N,
)


DATA_DIR = Path(os.getenv("PERSISTED_CACHE_DIR", "data_cache"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
PATHS = persisted_paths(DATA_DIR)
STATUS_PATH = DATA_DIR / "agent_status.json"


def log_progress(progress: float, stage: str, detail: str) -> None:
    print(f"[{progress:>5.1%}] {stage}: {detail}", flush=True)


def main() -> int:
    finnhub_key = os.getenv("FINNHUB_API_KEY", "").strip()
    sec_user_agent = os.getenv("SEC_USER_AGENT", "").strip()

    policy = RefreshPolicy(
        price_limit=int(os.getenv("AGENT_PRICE_LIMIT", "2200")),
        finnhub_limit=int(os.getenv("AGENT_FINNHUB_LIMIT", "1500")),
        finnhub_batch=int(os.getenv("AGENT_FINNHUB_BATCH", "40")),
        max_runtime_seconds=int(os.getenv("AGENT_MAX_SECONDS", "3000")),
    )

    status: dict[str, object] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "ok": False,
        "plan_before": refresh_plan(PATHS),
    }
    try:
        result = run_refresh_cycle(
            PATHS,
            finnhub_key=finnhub_key,
            sec_user_agent=sec_user_agent,
            policy=policy,
            progress_cb=log_progress,
        )

        snapshot = assemble_snapshot(
            result["universe"], result["sec"], result["finnhub"], result["yahoo"], result["prices"]
        )
        scored = add_scores(snapshot)
        eligible, full, lite = eligibility_masks(
            scored,
            min_completeness=DEFAULT_MIN_COMPLETENESS,
            min_confidence=DEFAULT_MIN_CONFIDENCE,
            allow_global_lite=GLOBAL_LITE_ENABLED_DEFAULT,
        )
        scored["history_rank"] = np.nan
        idx = scored.loc[eligible].sort_values(
            ["ranking_score", "score_total", "effective_data_confidence"], ascending=False
        ).index
        scored.loc[idx, "history_rank"] = np.arange(1, len(idx) + 1)
        append_snapshot(
            scored.loc[eligible].sort_values(
                ["ranking_score", "score_total", "effective_data_confidence"], ascending=False
            ),
            PATHS["history"],
            top_n=HISTORY_TOP_N,
            keep_days=HISTORY_KEEP_DAYS,
        )

        coverage = coverage_summary(
            scored,
            min_completeness=DEFAULT_MIN_COMPLETENESS,
            min_confidence=DEFAULT_MIN_CONFIDENCE,
            allow_global_lite=GLOBAL_LITE_ENABLED_DEFAULT,
        )
        status.update({
            "ok": True,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": result["elapsed_seconds"],
            "counters": result["counters"],
            "coverage": coverage,
            "plan_after": result.get("remaining") or refresh_plan(PATHS),
            "tier_counts": {
                "full": int(full.sum()),
                "global_lite": int(lite.sum()),
                "eligible": int(eligible.sum()),
            },
            "messages": [m for _, m in result["messages"]],
            "cache_rows": {
                "universe": len(result["universe"]),
                "sec": len(result["sec"]),
                "finnhub": len(result["finnhub"]),
                "prices": len(result["prices"]),
                "history": len(load_history(PATHS["history"])),
            },
        })
    except Exception as exc:
        status.update({
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "error": str(exc),
        })
        STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        raise

    STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(status, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
