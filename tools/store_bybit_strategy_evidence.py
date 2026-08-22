from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from app.strategy.crypto_live_evidence_postgres import (
    PostgresCryptoLiveEvidenceStore,
    evidence_report_id,
    extract_evidence_report,
)

_DSN_ENV = "BYBIT_OPPORTUNITY_DATABASE_DSN"


class _EvidenceStore(Protocol):
    def migrate(self) -> None: ...

    def persist_evidence_report(
        self,
        report: Mapping[str, Any],
        *,
        observed_at: datetime,
    ) -> str: ...


def prepare_evidence_import(
    payload: Mapping[str, Any],
    *,
    observed_at: datetime | None = None,
) -> tuple[Mapping[str, Any], datetime, str]:
    """Normalize direct matrix or full research artifact before durable import."""

    report, embedded = extract_evidence_report(payload)
    effective = embedded if embedded is not None else observed_at
    if effective is None:
        raise ValueError(
            "direct strategy evidence matrix requires explicit timezone-aware observed_at"
        )
    if effective.tzinfo is None or effective.utcoffset() is None:
        raise ValueError("strategy evidence observed_at must be timezone-aware")
    normalized = effective.astimezone(UTC)
    return report, normalized, evidence_report_id(report)


def store_evidence_payload(
    payload: Mapping[str, Any],
    *,
    store: _EvidenceStore,
    observed_at: datetime | None = None,
    migrate: bool = False,
) -> str:
    report, effective, expected_id = prepare_evidence_import(
        payload,
        observed_at=observed_at,
    )
    if migrate:
        store.migrate()
    persisted_id = store.persist_evidence_report(report, observed_at=effective)
    if persisted_id != expected_id:
        raise ValueError("persisted strategy evidence id differs from canonical report id")
    return persisted_id


def _load_json(path: Path) -> Mapping[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("strategy evidence artifact JSON must be an object")
    return payload


def _parse_optional_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("--observed-at must be timezone-aware")
    return parsed.astimezone(UTC)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Store a qualified Bybit strategy evidence matrix in append-only PostgreSQL. "
            "Accepts either the full dynamic Top-10 research artifact or the matrix payload."
        )
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--observed-at")
    parser.add_argument("--migrate-postgres", action="store_true")
    parser.add_argument("--database-dsn-env", default=_DSN_ENV)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    dsn = os.environ.get(args.database_dsn_env, "").strip()
    if not dsn:
        raise RuntimeError(
            f"required PostgreSQL DSN environment variable is missing:{args.database_dsn_env}"
        )
    payload = _load_json(args.input)
    evidence_id = store_evidence_payload(
        payload,
        store=PostgresCryptoLiveEvidenceStore(dsn),
        observed_at=_parse_optional_time(args.observed_at),
        migrate=args.migrate_postgres,
    )
    summary = {
        "evidence_snapshot_id": evidence_id,
        "postgres_persisted": True,
        "trade_actionable": False,
        "strategy_promotion_allowed": False,
        "bybit_live_order_routing_allowed": False,
    }
    print("BYBIT_STRATEGY_EVIDENCE_IMPORT=" + json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
