from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.strategy.crypto_prospective_evidence_materialization import (
    build_prospective_evidence_materialization_metadata,
)
from app.strategy.crypto_prospective_evidence_postgres import (
    PostgresCryptoProspectiveEvidenceStore,
)
from app.strategy.crypto_prospective_exact_cell_matrix import (
    CryptoProspectiveExactCellPolicy,
    diagnose_crypto_prospective_exact_cell_matrix,
)
from app.strategy.crypto_prospective_exact_cell_matrix_postgres import (
    PostgresCryptoProspectiveExactCellReader,
)

_DEFAULT_DSN_ENV = "BYBIT_OPPORTUNITY_DATABASE_DSN"


def build_prospective_exact_cell_report(
    *,
    dsn: str,
    rolling_days: int | None = None,
    maximum_final_seeds: int = 100_000,
) -> dict[str, object]:
    if not dsn.strip():
        raise ValueError("prospective exact-cell database DSN is required")
    if rolling_days is not None and not 1 <= rolling_days <= 3650:
        raise ValueError("prospective exact-cell rolling days must be within [1, 3650]")
    now = datetime.now(UTC)
    start = None if rolling_days is None else now - timedelta(days=rolling_days)
    reader = PostgresCryptoProspectiveExactCellReader(dsn)
    dataset = reader.load_dataset(
        signal_available_at_or_after=start,
        maximum_final_seeds=maximum_final_seeds,
    )
    report = diagnose_crypto_prospective_exact_cell_matrix(
        dataset,
        policy=CryptoProspectiveExactCellPolicy(),
    )
    report["report_generated_at"] = now.isoformat()
    report["report_window"] = (
        "ALL_AVAILABLE_PROSPECTIVE_HISTORY"
        if rolling_days is None
        else f"ROLLING_{rolling_days}_DAYS"
    )
    report["signal_available_at_or_after"] = None if start is None else start.isoformat()
    report["source_lineage"] = build_prospective_evidence_materialization_metadata(
        dataset,
        generated_at=now,
    )
    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build the prospective symbol x side x regime exact evidence-cell matrix from "
            "immutable source-time v111 context and final v112 outcomes. Read-only only."
        )
    )
    parser.add_argument("--rolling-days", type=int)
    parser.add_argument("--maximum-final-seeds", type=int, default=100_000)
    parser.add_argument("--output")
    parser.add_argument("--persist-postgres", action="store_true")
    parser.add_argument("--migrate-postgres", action="store_true")
    parser.add_argument("--database-dsn-env", default=_DEFAULT_DSN_ENV)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.migrate_postgres and not args.persist_postgres:
        raise SystemExit("--migrate-postgres requires --persist-postgres")
    dsn = os.environ.get(args.database_dsn_env, "")
    if not dsn.strip():
        raise SystemExit(
            "required PostgreSQL DSN environment variable is missing:"
            + args.database_dsn_env
        )
    report = build_prospective_exact_cell_report(
        dsn=dsn,
        rolling_days=args.rolling_days,
        maximum_final_seeds=args.maximum_final_seeds,
    )
    encoded = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(encoded + "\n", encoding="utf-8")
        temporary.replace(destination)
    if args.persist_postgres:
        store = PostgresCryptoProspectiveEvidenceStore(dsn)
        if args.migrate_postgres:
            store.migrate()
        store.persist(report)
    print(encoded)


if __name__ == "__main__":
    main()
