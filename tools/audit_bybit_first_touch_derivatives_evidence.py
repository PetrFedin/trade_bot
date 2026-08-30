from __future__ import annotations

import argparse
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.marketdata.bybit_derivatives_history import (
    BybitDerivativesHistory,
    BybitHistoricalDerivativesClient,
)
from app.strategy.crypto_first_touch_derivatives_evidence import (
    CryptoFirstTouchDerivativesEvidencePolicy,
    build_crypto_first_touch_derivatives_evidence,
    diagnose_crypto_first_touch_derivatives_evidence,
)
from tools.replay_bybit_crypto import default_crypto_config


def run_first_touch_derivatives_evidence(
    first_touch_report: dict[str, Any],
    *,
    derivatives_workers: int = 4,
    interval: str = "1h",
    evidence_policy: CryptoFirstTouchDerivativesEvidencePolicy | None = None,
) -> dict[str, Any]:
    _validate_upstream_report(first_touch_report)
    if not 1 <= derivatives_workers <= 8:
        raise ValueError("first-touch derivatives workers must be within [1, 8]")
    episode_rows = first_touch_report["episode_outcome_rows"]
    if not isinstance(episode_rows, list):
        raise ValueError("first-touch derivatives requires episode outcome rows")
    symbols = tuple(first_touch_report["symbols"])
    dates = tuple(date.fromisoformat(value) for value in first_touch_report["archive_dates"])
    if not dates:
        raise ValueError("first-touch derivatives requires archive dates")
    start_at = datetime.combine(min(dates), datetime.min.time(), tzinfo=UTC)
    end_at = datetime.combine(max(dates) + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    start_ms = int(start_at.timestamp() * 1000)
    end_ms = int(end_at.timestamp() * 1000) - 1
    histories = _fetch_histories(
        symbols=symbols,
        start_ms=start_ms,
        end_ms=end_ms,
        interval=interval,
        workers=derivatives_workers,
    )
    rows = build_crypto_first_touch_derivatives_evidence(
        episode_rows,
        histories,
        strategy_config=default_crypto_config().with_target(Decimal("20")),
    )
    report = diagnose_crypto_first_touch_derivatives_evidence(
        rows,
        policy=evidence_policy,
    )
    report.update(
        source="BYBIT_V5_PUBLIC_DERIVATIVES_HISTORY_JOINED_TO_FROZEN_FIRST_TOUCH_EPISODES",
        upstream_first_touch_audit=first_touch_report["audit"],
        upstream_archive_dates=list(first_touch_report["archive_dates"]),
        upstream_symbols=list(symbols),
        upstream_plan_eligible_signal_count=first_touch_report[
            "plan_eligible_signal_count"
        ],
        upstream_independent_episode_count=first_touch_report[
            "independent_episode_count"
        ],
        derivatives_interval=interval,
        derivatives_start_ms=start_ms,
        derivatives_end_ms=end_ms,
        derivatives_request_count=sum(history.request_count for history in histories.values()),
        derivatives_hosts=sorted({history.host for history in histories.values()}),
        public_get_only=True,
        upstream_outcomes_recomputed=False,
        raw_order_writes_supported=False,
    )
    return report


def _fetch_histories(
    *,
    symbols: tuple[str, ...],
    start_ms: int,
    end_ms: int,
    interval: str,
    workers: int,
) -> dict[str, BybitDerivativesHistory]:
    def fetch_one(symbol: str) -> tuple[str, BybitDerivativesHistory]:
        history = BybitHistoricalDerivativesClient().fetch_history(
            symbol=symbol,
            start_ms=start_ms,
            end_ms=end_ms,
            interval=interval,
        )
        history.validate()
        return symbol, history

    with ThreadPoolExecutor(max_workers=min(workers, len(symbols))) as executor:
        result = dict(executor.map(fetch_one, symbols))
    if set(result) != set(symbols):
        raise ValueError("first-touch derivatives history coverage is incomplete")
    return result


def _validate_upstream_report(report: dict[str, Any]) -> None:
    if report.get("audit") != "BYBIT_CRYPTO_PLAN_ELIGIBLE_FIRST_TOUCH_V2":
        raise ValueError("first-touch derivatives requires exact V2 upstream audit")
    for field in (
        "strategy_selection_allowed",
        "strategy_promotion_allowed",
        "demo_activation_allowed",
        "live_activation_allowed",
        "bybit_live_order_routing_allowed",
        "predictive_guarantee_allowed",
    ):
        if report.get(field) is not False:
            raise ValueError(f"first-touch derivatives requires upstream {field}=false")
    rows = report.get("episode_outcome_rows")
    if not isinstance(rows, list):
        raise ValueError("first-touch derivatives upstream episode rows are missing")
    if len(rows) != report.get("independent_episode_count"):
        raise ValueError("first-touch derivatives upstream episode count is inconsistent")
    identities = {
        (row.get("symbol"), row.get("side"), row.get("signal_available_at"))
        for row in rows
        if isinstance(row, dict)
    }
    if len(identities) != len(rows):
        raise ValueError("first-touch derivatives upstream episode identities are not unique")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Join frozen Bybit first-touch episodes to point-in-time derivatives evidence"
    )
    parser.add_argument("--first-touch-report", type=Path, required=True)
    parser.add_argument("--derivatives-workers", type=int, default=4)
    parser.add_argument("--interval", default="1h")
    parser.add_argument("--minimum-cell-episodes", type=int, default=5)
    parser.add_argument("--sample-sufficient-episodes", type=int, default=30)
    parser.add_argument("--minimum-cross-symbol-count", type=int, default=2)
    parser.add_argument("--minimum-distinct-days", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    raw = args.first_touch_report.read_bytes()
    upstream_sha256 = hashlib.sha256(raw).hexdigest()
    upstream = json.loads(raw)
    policy = CryptoFirstTouchDerivativesEvidencePolicy(
        minimum_cell_episodes=args.minimum_cell_episodes,
        sample_sufficient_episodes=args.sample_sufficient_episodes,
        minimum_cross_symbol_count=args.minimum_cross_symbol_count,
        minimum_distinct_days=args.minimum_distinct_days,
    )
    report = run_first_touch_derivatives_evidence(
        upstream,
        derivatives_workers=args.derivatives_workers,
        interval=args.interval,
        evidence_policy=policy,
    )
    report["upstream_first_touch_report_sha256"] = upstream_sha256
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        "BYBIT_FIRST_TOUCH_DERIVATIVES_EVIDENCE="
        + json.dumps(
            {
                "episode_count": report["episode_count"],
                "complete_context_count": report["complete_context_count"],
                "complete_context_fraction": report["complete_context_fraction"],
                "perfect_cross_token_cell_count": report[
                    "perfect_cross_token_cell_count"
                ],
                "perfect_exact_symbol_cell_count": report[
                    "perfect_exact_symbol_cell_count"
                ],
                "qualified_cross_token_cells": report[
                    "qualified_cross_token_cells"
                ],
                "retrospective_perfect_cross_token_cells": report[
                    "retrospective_perfect_cross_token_cells"
                ],
                "output": str(args.output),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
