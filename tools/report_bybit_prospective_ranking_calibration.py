from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.strategy.crypto_prospective_calibration_postgres import (
    PostgresCryptoProspectiveCalibrationReader,
)
from app.strategy.crypto_prospective_ranking_calibration import (
    diagnose_crypto_prospective_ranking_calibration,
)

_DSN_ENV = "BYBIT_OPPORTUNITY_DATABASE_DSN"


def build_prospective_ranking_calibration_report(
    reader: PostgresCryptoProspectiveCalibrationReader,
    *,
    observed_at: datetime | None = None,
    since_days: int | None = None,
    maximum_final_seeds: int = 100_000,
) -> dict[str, Any]:
    cutoff = _utc(datetime.now(UTC) if observed_at is None else observed_at)
    if since_days is not None:
        if isinstance(since_days, bool) or not 1 <= since_days <= 3650:
            raise ValueError("prospective calibration since_days must be within [1, 3650]")
        start = cutoff - timedelta(days=since_days)
    else:
        start = None
    dataset = reader.load_dataset(
        signal_available_at_or_after=start,
        maximum_final_seeds=maximum_final_seeds,
    )
    calibration = diagnose_crypto_prospective_ranking_calibration(dataset)
    return {
        "report": "BYBIT_PROSPECTIVE_RANKING_CALIBRATION_REPORT",
        "observed_at": cutoff.isoformat(),
        "window_start_at": None if start is None else start.isoformat(),
        "window_mode": "ALL_AVAILABLE_PROSPECTIVE_HISTORY" if start is None else "ROLLING_DAYS",
        "calibration": calibration,
        "source": "POSTGRES_V111_LIVE_RANKING_PLUS_V112_PROSPECTIVE_SHADOW_OUTCOMES",
        "trade_actionable": False,
        "operator_review_required": True,
        "parameter_retuning_performed": False,
        "ranking_weights_changed": False,
        "strategy_promotion_allowed": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
        "causal_claim_allowed": False,
        "statistical_significance_claim_allowed": False,
        "predictive_guarantee_allowed": False,
    }


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("prospective calibration report timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Measure out-of-sample calibration of Bybit live evidence ranking from final "
            "prospective 15m/60m/240m shadow outcomes."
        )
    )
    parser.add_argument("--dsn-env", default=_DSN_ENV)
    parser.add_argument("--since-days", type=int)
    parser.add_argument("--maximum-final-seeds", type=int, default=100_000)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    dsn = os.getenv(args.dsn_env, "").strip()
    if not dsn:
        raise RuntimeError("prospective calibration PostgreSQL DSN environment is missing")
    report = build_prospective_ranking_calibration_report(
        PostgresCryptoProspectiveCalibrationReader(dsn),
        since_days=args.since_days,
        maximum_final_seeds=args.maximum_final_seeds,
    )
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output is None:
        print(text)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
