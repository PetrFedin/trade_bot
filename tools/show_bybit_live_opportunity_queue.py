from __future__ import annotations

import argparse
import json
import os

from app.strategy.crypto_live_opportunity_reader import PostgresCryptoLiveOpportunityReader

_DSN_ENV = "BYBIT_OPPORTUNITY_DATABASE_DSN"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read the latest evidence-ranked Bybit operator review queue from PostgreSQL. "
            "This command has no order-writing surface."
        )
    )
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--include-mixed", action="store_true")
    parser.add_argument("--database-dsn-env", default=_DSN_ENV)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    dsn = os.environ.get(args.database_dsn_env, "").strip()
    if not dsn:
        raise RuntimeError(
            f"required PostgreSQL DSN environment variable is missing:{args.database_dsn_env}"
        )
    reader = PostgresCryptoLiveOpportunityReader(dsn)
    rows = reader.latest_review_queue(
        limit=args.limit,
        include_mixed=args.include_mixed,
    )
    payload = {
        "queue": [dict(row) for row in rows],
        "queue_count": len(rows),
        "include_mixed": args.include_mixed,
        "operator_review_required": True,
        "trade_actionable": False,
        "bybit_live_order_routing_allowed": False,
    }
    print("BYBIT_LIVE_OPPORTUNITY_REVIEW_QUEUE=" + json.dumps(payload, default=str, sort_keys=True))


if __name__ == "__main__":
    main()
