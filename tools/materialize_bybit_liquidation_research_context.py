from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import UTC, datetime

from app.strategy.crypto_prospective_liquidation_postgres import (
    PostgresProspectiveLiquidationContextStore,
)

_DEFAULT_DSN_ENV = "BYBIT_OPPORTUNITY_DATABASE_DSN"


def materialize_liquidation_research_context(
    *,
    dsn: str,
    limit: int = 100,
    minimum_signal_age_seconds: int = 120,
    maximum_status_age_seconds: int = 60,
    migrate: bool = False,
) -> dict[str, object]:
    if not dsn.strip():
        raise ValueError("liquidation research database DSN is required")
    store = PostgresProspectiveLiquidationContextStore(dsn)
    if migrate:
        store.migrate()
    evaluated_at = datetime.now(UTC)
    contexts = store.attach_pending(
        evaluated_at=evaluated_at,
        limit=limit,
        minimum_signal_age_seconds=minimum_signal_age_seconds,
        maximum_status_age_seconds=maximum_status_age_seconds,
    )
    blockers: Counter[str] = Counter()
    qualified = 0
    known_zero_windows = 0
    for context in contexts:
        if context.coverage_qualified:
            qualified += 1
        else:
            blockers.update(context.coverage_reason_codes)
        known_zero_windows += sum(item.known_zero for item in context.windows)
    return {
        "schema": "BYBIT_LIQUIDATION_RESEARCH_CONTEXT_V117",
        "evaluated_at": evaluated_at.isoformat(),
        "materialized_count": len(contexts),
        "coverage_qualified_count": qualified,
        "coverage_unqualified_count": len(contexts) - qualified,
        "coverage_reason_counts": dict(sorted(blockers.items())),
        "known_zero_window_count": known_zero_windows,
        "research_only": True,
        "liquidation_feature_used_for_source_ranking": False,
        "parameter_retuning_performed": False,
        "trade_actionable": False,
        "strategy_promotion_allowed": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Materialize coverage-qualified pre-signal liquidation research context "
            "for existing prospective observations. PostgreSQL only."
        )
    )
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--minimum-signal-age-seconds", type=int, default=120)
    parser.add_argument("--maximum-status-age-seconds", type=int, default=60)
    parser.add_argument("--migrate-postgres", action="store_true")
    parser.add_argument("--database-dsn-env", default=_DEFAULT_DSN_ENV)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    dsn = os.environ.get(args.database_dsn_env, "")
    if not dsn.strip():
        raise SystemExit(
            "required PostgreSQL DSN environment variable is missing:"
            + args.database_dsn_env
        )
    report = materialize_liquidation_research_context(
        dsn=dsn,
        limit=args.limit,
        minimum_signal_age_seconds=args.minimum_signal_age_seconds,
        maximum_status_age_seconds=args.maximum_status_age_seconds,
        migrate=args.migrate_postgres,
    )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
