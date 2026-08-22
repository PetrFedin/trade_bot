from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from app.marketdata.bybit_opportunity_postgres import PostgresBybitOpportunityStore
from app.marketdata.bybit_opportunity_registry import (
    BybitOpportunitySnapshot,
    build_bybit_opportunity_snapshot,
)
from app.marketdata.bybit_research_universe import (
    BybitResearchInstrument,
    BybitResearchTicker,
    BybitResearchUniverseClient,
    BybitResearchUniversePolicy,
)

_SITE_HOSTS = {
    "global": "api.bybit.com",
    "global-alt": "api.bytick.com",
    "nl": "api.bybit.nl",
    "tr": "api.bybit.tr",
    "kz": "api.bybit.kz",
    "georgia": "api.bybitgeorgia.ge",
    "ae": "api.bybit.ae",
    "eu": "api.bybit.eu",
    "id": "api.bybit.id",
    "jp": "api.manepa.jp",
    "hk": "api-spark-fintech.com",
}
_DEFAULT_DSN_ENV = "BYBIT_OPPORTUNITY_DATABASE_DSN"


class _UniverseClient(Protocol):
    def fetch_instruments(self) -> tuple[BybitResearchInstrument, ...]: ...

    def fetch_tickers(self) -> tuple[BybitResearchTicker, ...]: ...


def run_public_opportunity_snapshot(
    *,
    observed_at: datetime | None = None,
    bybit_site: str = "global",
    registry_limit: int = 50,
    universe_policy: BybitResearchUniversePolicy | None = None,
    universe_client: _UniverseClient | None = None,
) -> BybitOpportunitySnapshot:
    host = _site_host(bybit_site)
    client = (
        BybitResearchUniverseClient(host=host)
        if universe_client is None
        else universe_client
    )
    instruments = client.fetch_instruments()
    tickers = client.fetch_tickers()
    cutoff = datetime.now(UTC) if observed_at is None else _utc(observed_at)
    snapshot = build_bybit_opportunity_snapshot(
        instruments,
        tickers,
        observed_at_ms=int(cutoff.timestamp() * 1000),
        host=host,
        universe_policy=universe_policy,
        registry_limit=registry_limit,
    )
    snapshot.validate()
    return snapshot


def persist_snapshot_from_env(
    snapshot: BybitOpportunitySnapshot,
    *,
    dsn_env: str = _DEFAULT_DSN_ENV,
    migrate: bool = False,
) -> str:
    if not dsn_env or dsn_env != dsn_env.strip():
        raise ValueError("Bybit opportunity DSN environment variable name is invalid")
    dsn = os.environ.get(dsn_env, "")
    if not dsn.strip():
        raise RuntimeError(f"required PostgreSQL DSN environment variable is missing:{dsn_env}")
    store = PostgresBybitOpportunityStore(dsn)
    if migrate:
        store.migrate()
    return store.persist(snapshot)


def write_snapshot(snapshot: BybitOpportunitySnapshot, output: Path) -> None:
    payload = snapshot.to_payload()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)


def _site_host(site: str) -> str:
    normalized = site.strip().lower()
    if normalized != site or normalized not in _SITE_HOSTS:
        raise ValueError("Bybit opportunity site must be one of " + ",".join(sorted(_SITE_HOSTS)))
    return _SITE_HOSTS[normalized]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Bybit opportunity observed_at must be timezone-aware")
    return value.astimezone(UTC)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Capture a public Bybit ranked research registry. The output cannot activate "
            "demo or live order routing."
        )
    )
    parser.add_argument(
        "--site",
        default=os.environ.get("BYBIT_MAINNET_READONLY_SITE", "global"),
        choices=sorted(_SITE_HOSTS),
    )
    parser.add_argument("--registry-limit", type=int, default=50)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--persist-postgres", action="store_true")
    parser.add_argument("--migrate-postgres", action="store_true")
    parser.add_argument("--database-dsn-env", default=_DEFAULT_DSN_ENV)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.migrate_postgres and not args.persist_postgres:
        raise SystemExit("--migrate-postgres requires --persist-postgres")
    snapshot = run_public_opportunity_snapshot(
        bybit_site=args.site,
        registry_limit=args.registry_limit,
    )
    write_snapshot(snapshot, args.output)
    persisted = False
    if args.persist_postgres:
        persist_snapshot_from_env(
            snapshot,
            dsn_env=args.database_dsn_env,
            migrate=args.migrate_postgres,
        )
        persisted = True
    summary = {
        "snapshot_id": snapshot.snapshot_id,
        "observed_at_ms": snapshot.observed_at_ms,
        "host": snapshot.host,
        "top10_complete": snapshot.top10_complete,
        "top10_symbols": list(snapshot.top10_symbols),
        "eligible_symbol_count": snapshot.eligible_symbol_count,
        "registry_candidate_count": len(snapshot.candidates),
        "postgres_persisted": persisted,
        "trade_actionable": False,
        "bybit_live_order_routing_allowed": False,
    }
    print("BYBIT_OPPORTUNITY_REGISTRY=" + json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
