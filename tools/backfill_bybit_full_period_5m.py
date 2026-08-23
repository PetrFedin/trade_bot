from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.marketdata.bybit_full_period_5m import (
    BybitFullPeriod5mCoveragePlan,
    build_bybit_full_period_5m_plan,
)
from app.marketdata.bybit_full_period_5m_postgres import (
    BybitFullPeriod5mStoredCoverage,
    PostgresBybitFullPeriod5mStore,
)
from app.marketdata.bybit_public_archive import (
    BybitArchiveAcquisition,
    BybitArchiveUnavailableError,
    BybitPublicTradeArchiveClient,
)
from app.marketdata.bybit_research_universe import (
    BybitResearchUniverseClient,
    BybitResearchUniversePolicy,
    select_bybit_research_universe,
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
_DSN_ENV = "BYBIT_OPPORTUNITY_DATABASE_DSN"
_RETRY_DELAY = timedelta(hours=24)


class _UniverseClient(Protocol):
    def fetch_instruments(self): ...

    def fetch_tickers(self): ...


class _ArchiveClient(Protocol):
    def fetch_klines(
        self,
        *,
        symbols: tuple[str, ...],
        dates: tuple,
        interval_minutes: int = 5,
    ) -> BybitArchiveAcquisition: ...


class _Store(Protocol):
    def coverage_state(self, symbols) -> BybitFullPeriod5mStoredCoverage: ...

    def persist_complete_day(
        self,
        *,
        symbol,
        archive_date,
        acquisition,
        observed_at,
    ) -> str: ...

    def persist_unavailable(
        self,
        *,
        symbol,
        archive_date,
        error_code,
        retry_after,
        observed_at,
    ) -> str: ...


@dataclass(frozen=True)
class BybitFullPeriod5mBackfillSummary:
    observed_at: str
    bybit_site: str
    universe_host: str
    top10_symbols: tuple[str, ...]
    plan_id: str
    expected_day_count: int
    completed_day_count: int
    blocked_day_count: int
    pending_day_count: int
    coverage_fraction: str
    full_period_complete: bool
    attempted_day_count: int
    completed_this_run: int
    unavailable_this_run: int
    full_period_claim_allowed: bool
    trade_actionable: bool = False
    demo_activation_allowed: bool = False
    live_activation_allowed: bool = False
    bybit_live_order_routing_allowed: bool = False


def run_full_period_5m_backfill_cycle(
    store: _Store,
    *,
    observed_at: datetime | None = None,
    bybit_site: str = "global",
    maximum_days_per_run: int = 20,
    universe_client: _UniverseClient | None = None,
    archive_client: _ArchiveClient | None = None,
) -> tuple[BybitFullPeriod5mCoveragePlan, BybitFullPeriod5mBackfillSummary]:
    cutoff = _utc(datetime.now(UTC) if observed_at is None else observed_at)
    if isinstance(maximum_days_per_run, bool) or not 1 <= maximum_days_per_run <= 5000:
        raise ValueError("full-period 5m maximum days per run must be within [1, 5000]")
    host = _site_host(bybit_site)
    universe = (
        BybitResearchUniverseClient(host=host)
        if universe_client is None
        else universe_client
    )
    instruments = tuple(universe.fetch_instruments())
    tickers = tuple(universe.fetch_tickers())
    policy = BybitResearchUniversePolicy()
    selection = select_bybit_research_universe(
        instruments,
        tickers,
        observed_at_ms=int(cutoff.timestamp() * 1000),
        host=host,
        policy=policy,
    )
    if not selection.complete_top_n:
        raise RuntimeError(
            "full-period 5m backfill refused incomplete Top-10 universe:"
            + ",".join(selection.blockers)
        )
    top10_symbols = tuple(item.symbol for item in selection.selected)
    if len(top10_symbols) != 10:
        raise RuntimeError("full-period 5m backfill requires exactly ten selected symbols")
    coverage_symbols = tuple(sorted(top10_symbols))
    stored = store.coverage_state(coverage_symbols)
    plan = build_bybit_full_period_5m_plan(
        instruments,
        symbols=coverage_symbols,
        observed_at=cutoff,
        completed_by_symbol=stored.completed_by_symbol,
        unavailable_retry_after_by_symbol=stored.unavailable_retry_after_by_symbol,
    )
    work = plan.next_work_items(limit=maximum_days_per_run)
    archive = (
        BybitPublicTradeArchiveClient()
        if archive_client is None
        else archive_client
    )
    completed_this_run = 0
    unavailable_this_run = 0
    for item in work:
        try:
            acquisition = archive.fetch_klines(
                symbols=(item.symbol,),
                dates=(item.archive_date,),
                interval_minutes=5,
            )
            acquisition.validate(requested_symbols=(item.symbol,), minimum_bars=1)
            store.persist_complete_day(
                symbol=item.symbol,
                archive_date=item.archive_date,
                acquisition=acquisition,
                observed_at=cutoff,
            )
            completed_this_run += 1
        except BybitArchiveUnavailableError as exc:
            store.persist_unavailable(
                symbol=item.symbol,
                archive_date=item.archive_date,
                error_code=_archive_error_code(exc),
                retry_after=cutoff + _RETRY_DELAY,
                observed_at=cutoff,
            )
            unavailable_this_run += 1

    refreshed = store.coverage_state(coverage_symbols)
    final_plan = build_bybit_full_period_5m_plan(
        instruments,
        symbols=coverage_symbols,
        observed_at=cutoff,
        completed_by_symbol=refreshed.completed_by_symbol,
        unavailable_retry_after_by_symbol=refreshed.unavailable_retry_after_by_symbol,
    )
    summary = BybitFullPeriod5mBackfillSummary(
        observed_at=cutoff.isoformat(),
        bybit_site=bybit_site,
        universe_host=host,
        top10_symbols=top10_symbols,
        plan_id=final_plan.plan_id,
        expected_day_count=final_plan.expected_day_count,
        completed_day_count=final_plan.completed_day_count,
        blocked_day_count=final_plan.blocked_day_count,
        pending_day_count=final_plan.pending_day_count,
        coverage_fraction=str(final_plan.coverage_fraction),
        full_period_complete=final_plan.full_period_complete,
        attempted_day_count=len(work),
        completed_this_run=completed_this_run,
        unavailable_this_run=unavailable_this_run,
        full_period_claim_allowed=final_plan.full_period_complete,
    )
    return final_plan, summary


def _archive_error_code(exc: BybitArchiveUnavailableError) -> str:
    message = str(exc)
    if "HTTP_404" in message:
        return "ARCHIVE_HTTP_404"
    if "HTTP_403" in message:
        return "ARCHIVE_HTTP_403"
    if "TRANSPORT" in message:
        return "ARCHIVE_TRANSPORT"
    if "no trades" in message:
        return "ARCHIVE_EMPTY"
    return "ARCHIVE_UNAVAILABLE"


def _site_host(site: str) -> str:
    normalized = site.strip().lower()
    if normalized != site or normalized not in _SITE_HOSTS:
        raise ValueError("Bybit site must be one of " + ",".join(sorted(_SITE_HOSTS)))
    return _SITE_HOSTS[normalized]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("full-period 5m backfill timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Incrementally backfill official Bybit trade archives into an append-only 5m "
            "PostgreSQL history. Full-period claims remain disabled until coverage is 100%."
        )
    )
    parser.add_argument(
        "--site",
        default=os.environ.get("BYBIT_MAINNET_READONLY_SITE", "global"),
        choices=sorted(_SITE_HOSTS),
    )
    parser.add_argument("--maximum-days-per-run", type=int, default=20)
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
    store = PostgresBybitFullPeriod5mStore(dsn)
    if args.migrate_postgres:
        store.migrate()
    plan, summary = run_full_period_5m_backfill_cycle(
        store,
        bybit_site=args.site,
        maximum_days_per_run=args.maximum_days_per_run,
    )
    result = asdict(summary)
    result["coverage"] = [item.to_payload() for item in plan.coverage]
    print("BYBIT_FULL_PERIOD_5M_BACKFILL=" + json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
