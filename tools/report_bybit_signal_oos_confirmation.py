from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from app.strategy.crypto_prospective_exact_cell_matrix_postgres import (
    PostgresCryptoProspectiveExactCellReader,
)
from app.strategy.crypto_signal_oos_confirmation import (
    CryptoSignalOosConfirmationPolicy,
    confirm_crypto_historical_perfect_cells_oos,
)
from app.strategy.crypto_signal_oos_confirmation_postgres import (
    PostgresCryptoHistoricalPerfectEvidenceReader,
)

_DEFAULT_DSN_ENV = "BYBIT_OPPORTUNITY_DATABASE_DSN"


def build_signal_oos_confirmation_report(
    *,
    dsn: str,
    evidence_snapshot_id: str,
    minimum_historical_trades: int = 5,
    minimum_oos_observations: int = 30,
    maximum_final_seeds: int = 100_000,
) -> dict[str, object]:
    if not dsn.strip():
        raise ValueError("signal OOS database DSN is required")
    if not 1 <= maximum_final_seeds <= 1_000_000:
        raise ValueError("signal OOS maximum final seeds is invalid")
    historical_reader = PostgresCryptoHistoricalPerfectEvidenceReader(dsn)
    snapshot = historical_reader.load_snapshot(evidence_snapshot_id)
    prospective_reader = PostgresCryptoProspectiveExactCellReader(dsn)
    prospective = prospective_reader.load_dataset(
        signal_available_at_or_after=None,
        maximum_final_seeds=maximum_final_seeds,
    )
    return confirm_crypto_historical_perfect_cells_oos(
        snapshot,
        prospective,
        policy=CryptoSignalOosConfirmationPolicy(
            minimum_historical_trades=minimum_historical_trades,
            minimum_oos_observations=minimum_oos_observations,
        ),
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Confirm one frozen v111 historical-perfect exact-cell snapshot against strictly "
            "later final v112 prospective outcomes. Read-only; never changes ranking or orders."
        )
    )
    parser.add_argument("--evidence-snapshot-id", required=True)
    parser.add_argument("--minimum-historical-trades", type=int, default=5)
    parser.add_argument("--minimum-oos-observations", type=int, default=30)
    parser.add_argument("--maximum-final-seeds", type=int, default=100_000)
    parser.add_argument("--database-dsn-env", default=_DEFAULT_DSN_ENV)
    parser.add_argument("--output")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    dsn = os.environ.get(args.database_dsn_env, "")
    if not dsn.strip():
        raise SystemExit(
            "required PostgreSQL DSN environment variable is missing:" + args.database_dsn_env
        )
    report = build_signal_oos_confirmation_report(
        dsn=dsn,
        evidence_snapshot_id=args.evidence_snapshot_id,
        minimum_historical_trades=args.minimum_historical_trades,
        minimum_oos_observations=args.minimum_oos_observations,
        maximum_final_seeds=args.maximum_final_seeds,
    )
    encoded = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(encoded + "\n", encoding="utf-8")
        temporary.replace(destination)
    print(encoded)


if __name__ == "__main__":
    main()
