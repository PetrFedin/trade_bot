from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.runtime.bybit_mainnet_readonly_probe import (
    BybitMainnetReadOnlyCredentials,
    BybitMainnetReadOnlySnapshot,
    probe_bybit_mainnet_readonly_connection,
)
from app.strategy.crypto_live_evidence_postgres import PostgresCryptoLiveEvidenceStore
from app.strategy.crypto_readonly_account_context import (
    CryptoReadOnlyAccountAwareRegistrySnapshot,
    CryptoReadOnlyAccountContext,
    build_crypto_account_aware_registry_snapshot,
    build_crypto_readonly_account_context,
)
from app.strategy.crypto_readonly_account_postgres import (
    PostgresCryptoReadOnlyAccountContextStore,
)
from tools.refresh_bybit_live_evidence_registry import (
    persist_live_refresh,
    run_live_evidence_refresh,
)

_DSN_ENV = "BYBIT_OPPORTUNITY_DATABASE_DSN"


def run_mainnet_readonly_account_aware_refresh(
    *,
    evidence_report: Mapping[str, Any],
    mainnet_snapshot: BybitMainnetReadOnlySnapshot,
    observed_at: datetime,
    bybit_site: str,
    registry_limit: int = 50,
    universe_client: Any = None,
    kline_client: Any = None,
    derivatives_client: Any = None,
) -> tuple[Any, Any, CryptoReadOnlyAccountContext, CryptoReadOnlyAccountAwareRegistrySnapshot]:
    observed = _utc(observed_at)
    mainnet_snapshot.validate()
    account = build_crypto_readonly_account_context(
        mainnet_snapshot,
        observed_at=observed,
    )
    sizing_capital = account.sizing_capital_usd_equivalent
    if sizing_capital is None:
        raise RuntimeError("mainnet read-only account has no positive available sizing capital")
    if not account.position_exposure_complete:
        raise RuntimeError(
            "mainnet read-only account position exposure is incomplete:"
            + ",".join(account.position_exposure_missing_reasons)
        )
    market_snapshot, ranking = run_live_evidence_refresh(
        evidence_report=evidence_report,
        observed_at=observed,
        bybit_site=bybit_site,
        equity_usdt=sizing_capital,
        equity_source=account.equity_source,
        registry_limit=registry_limit,
        universe_client=universe_client,
        kline_client=kline_client,
        derivatives_client=derivatives_client,
    )
    account_aware = build_crypto_account_aware_registry_snapshot(
        ranking,
        account,
        observed_at=observed,
    )
    return market_snapshot, ranking, account, account_aware


def _write_json(payload: Mapping[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Refresh the Bybit evidence registry using a verified read-only mainnet "
            "account for sizing/exposure context. No order writes are supported."
        )
    )
    parser.add_argument("--registry-limit", type=int, default=50)
    parser.add_argument("--ranking-output", type=Path, required=True)
    parser.add_argument("--account-context-output", type=Path, required=True)
    parser.add_argument("--database-dsn-env", default=_DSN_ENV)
    parser.add_argument("--persist-postgres", action="store_true")
    parser.add_argument("--migrate-postgres", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if args.migrate_postgres and not args.persist_postgres:
        raise SystemExit("--migrate-postgres requires --persist-postgres")
    dsn = os.getenv(args.database_dsn_env, "").strip()
    if not dsn:
        raise RuntimeError(
            f"required PostgreSQL DSN environment variable is missing:{args.database_dsn_env}"
        )
    evidence_store = PostgresCryptoLiveEvidenceStore(dsn)
    evidence_report = evidence_store.latest_evidence_report()
    if evidence_report is None:
        raise RuntimeError("mainnet read-only refresh has no persisted evidence snapshot")

    credentials = BybitMainnetReadOnlyCredentials.from_env()
    client = credentials.build_client()
    mainnet_snapshot = probe_bybit_mainnet_readonly_connection(client)
    observed_at = datetime.now(UTC)
    market_snapshot, ranking, account, account_aware = (
        run_mainnet_readonly_account_aware_refresh(
            evidence_report=evidence_report,
            mainnet_snapshot=mainnet_snapshot,
            observed_at=observed_at,
            bybit_site=credentials.site,
            registry_limit=args.registry_limit,
        )
    )
    _write_json(ranking.to_payload(), args.ranking_output)
    _write_json(account_aware.to_payload(), args.account_context_output)

    postgres_persisted = False
    account_context_postgres_persisted = False
    if args.persist_postgres:
        persist_live_refresh(
            market_snapshot,
            ranking,
            evidence_report=evidence_report,
            evidence_observed_at=observed_at,
            dsn=dsn,
            migrate=args.migrate_postgres,
        )
        account_store = PostgresCryptoReadOnlyAccountContextStore(dsn)
        if args.migrate_postgres:
            account_store.migrate()
        account_store.persist(account_aware)
        postgres_persisted = True
        account_context_postgres_persisted = True

    summary = {
        "ranking_snapshot_id": ranking.snapshot_id,
        "account_context_snapshot_id": account_aware.snapshot_id,
        "equity_source": account.equity_source,
        "sizing_capital_usd_equivalent": str(account.sizing_capital_usd_equivalent),
        "total_equity_usd": str(account.total_equity_usd),
        "total_available_balance_usd": str(account.total_available_balance_usd),
        "gross_position_value_usd": str(account.gross_position_value_usd),
        "open_position_count": account.open_position_count,
        "qualified_positive_count": ranking.qualified_positive_count,
        "qualified_mixed_count": ranking.qualified_mixed_count,
        "postgres_persisted": postgres_persisted,
        "account_context_postgres_persisted": account_context_postgres_persisted,
        "read_only_verified": account.read_only_verified,
        "ip_binding_verified": account.ip_binding_verified,
        "operator_review_required": True,
        "trade_actionable": False,
        "order_writes_supported": False,
        "bybit_live_order_routing_allowed": False,
    }
    print("BYBIT_MAINNET_READONLY_EVIDENCE_REGISTRY=" + json.dumps(summary, sort_keys=True))
    return 0


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("mainnet read-only refresh timestamp must be timezone-aware")
    return value.astimezone(UTC)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
