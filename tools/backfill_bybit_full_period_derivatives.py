from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.marketdata.bybit_derivatives_history import BybitHistoricalDerivativesClient
from app.marketdata.bybit_full_period_derivatives import (
    ACCOUNT_RATIO,
    FUNDING,
    OPEN_INTEREST,
    audit_bybit_derivatives_source_day,
    build_bybit_full_period_derivatives_plan,
    derivatives_source_start_at,
)
from app.marketdata.bybit_full_period_derivatives_postgres import (
    PostgresBybitFullPeriodDerivativesStore,
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
_DSN_ENV = "BYBIT_FULL_PERIOD_DATABASE_DSN"
_RETRY_AFTER = timedelta(hours=6)


def run_full_period_derivatives_backfill(
    *,
    store: PostgresBybitFullPeriodDerivativesStore,
    observed_at: datetime | None = None,
    bybit_site: str = "global",
    work_limit: int = 24,
    migrate: bool = False,
    universe_client: BybitResearchUniverseClient | None = None,
    derivatives_client: BybitHistoricalDerivativesClient | None = None,
) -> dict[str, Any]:
    cutoff = datetime.now(UTC) if observed_at is None else _utc(observed_at)
    if isinstance(work_limit, bool) or not 1 <= work_limit <= 5000:
        raise ValueError("full-period derivatives work limit must be within [1, 5000]")
    host = _site_host(bybit_site)
    if migrate:
        store.migrate()
    universe = (
        BybitResearchUniverseClient(host=host)
        if universe_client is None
        else universe_client
    )
    derivatives = (
        BybitHistoricalDerivativesClient(host=host, maximum_pages_per_series=1000)
        if derivatives_client is None
        else derivatives_client
    )
    instruments = universe.fetch_instruments()
    tickers = universe.fetch_tickers()
    policy = BybitResearchUniversePolicy(top_n=10)
    selection = select_bybit_research_universe(
        instruments,
        tickers,
        observed_at_ms=int(cutoff.timestamp() * 1000),
        host=host,
        policy=policy,
    )
    if not selection.complete_top_n:
        raise RuntimeError("full-period derivatives backfill requires a complete dynamic Top-10")
    symbols = tuple(sorted(item.symbol for item in selection.selected))
    instrument_map = {item.symbol: item for item in instruments}
    if any(symbol not in instrument_map for symbol in symbols):
        raise RuntimeError("full-period derivatives selection lost instrument metadata")

    stored = store.coverage_state(symbols)
    initial_plan = build_bybit_full_period_derivatives_plan(
        instruments,
        symbols=symbols,
        observed_at=cutoff,
        completed_by_source_symbol=stored.completed_by_source_symbol,
        unavailable_retry_after_by_source_symbol=(
            stored.unavailable_retry_after_by_source_symbol
        ),
    )
    attempted = 0
    completed = 0
    unavailable = 0
    request_count = 0
    for work in initial_plan.next_work_items(limit=work_limit):
        attempted += 1
        instrument = instrument_map[work.symbol]
        source_start = derivatives_source_start_at(instrument, source=work.source)
        day_start = datetime.combine(
            work.archive_date,
            datetime.min.time(),
            tzinfo=UTC,
        )
        query_start = max(day_start, source_start)
        query_end = day_start + timedelta(days=1)
        start_ms = int(query_start.timestamp() * 1000)
        end_ms = int(query_end.timestamp() * 1000)
        try:
            if work.source == OPEN_INTEREST:
                points, requests = derivatives.fetch_open_interest(
                    symbol=work.symbol,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    interval="5min",
                )
            elif work.source == ACCOUNT_RATIO:
                points, requests = derivatives.fetch_account_ratio(
                    symbol=work.symbol,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    interval="5min",
                )
            elif work.source == FUNDING:
                points, requests = derivatives.fetch_funding(
                    symbol=work.symbol,
                    start_ms=start_ms,
                    end_ms=end_ms,
                )
            else:  # pragma: no cover - work item validates source
                raise RuntimeError("full-period derivatives work source is unsupported")
        except RuntimeError:
            store.persist_unavailable(
                source=work.source,
                symbol=work.symbol,
                archive_date=work.archive_date,
                query_start_at=query_start,
                query_end_at=query_end,
                error_code="BYBIT_PUBLIC_DERIVATIVES_REQUEST_FAILED",
                retry_after=cutoff + _RETRY_AFTER,
                observed_at=cutoff,
            )
            unavailable += 1
            continue
        request_count += requests
        audit = audit_bybit_derivatives_source_day(
            instrument,
            source=work.source,
            archive_date=work.archive_date,
            points=points,
            query_window_complete=True,
        )
        if not audit.complete:
            store.persist_unavailable(
                source=work.source,
                symbol=work.symbol,
                archive_date=work.archive_date,
                query_start_at=query_start,
                query_end_at=query_end,
                error_code="BYBIT_DERIVATIVES_SOURCE_DAY_INCOMPLETE",
                retry_after=cutoff + _RETRY_AFTER,
                observed_at=cutoff,
            )
            unavailable += 1
            continue
        store.persist_complete_day(
            audit=audit,
            points=points,
            observed_at=cutoff,
        )
        completed += 1

    refreshed = store.coverage_state(symbols)
    final_plan = build_bybit_full_period_derivatives_plan(
        instruments,
        symbols=symbols,
        observed_at=cutoff,
        completed_by_source_symbol=refreshed.completed_by_source_symbol,
        unavailable_retry_after_by_source_symbol=(
            refreshed.unavailable_retry_after_by_source_symbol
        ),
    )
    return {
        "diagnostic": "BYBIT_FULL_PERIOD_DERIVATIVES_BACKFILL_V114",
        "observed_at": cutoff.isoformat(),
        "host": host,
        "dynamic_top10": [
            {
                "rank": item.rank,
                "symbol": item.symbol,
                "universe_score": str(item.score),
            }
            for item in selection.selected
        ],
        "attempted_work_items": attempted,
        "completed_work_items": completed,
        "unavailable_work_items": unavailable,
        "public_request_count": request_count,
        "coverage_plan": final_plan.to_payload(),
        "full_period_evidence_matrix_allowed": (
            final_plan.full_period_evidence_matrix_allowed
        ),
        "trade_actionable": False,
        "strategy_parameters_changed": False,
        "strategy_promotion_allowed": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
        "causal_claim_allowed": False,
        "predictive_guarantee_allowed": False,
    }


def _site_host(site: str) -> str:
    normalized = site.strip().lower()
    try:
        return _SITE_HOSTS[normalized]
    except KeyError as exc:
        raise ValueError("unsupported Bybit research site") from exc


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("full-period derivatives observed_at must be timezone-aware")
    return value.astimezone(UTC)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Incrementally backfill current dynamic Bybit Top-10 derivatives history."
    )
    parser.add_argument("--site", default="global", choices=sorted(_SITE_HOSTS))
    parser.add_argument("--work-limit", type=int, default=24)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--migrate-postgres", action="store_true")
    parser.add_argument("--dsn-env", default=_DSN_ENV)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    dsn = os.getenv(args.dsn_env, "")
    if not dsn.strip():
        raise SystemExit(f"required PostgreSQL DSN env is missing: {args.dsn_env}")
    store = PostgresBybitFullPeriodDerivativesStore(dsn)
    payload = run_full_period_derivatives_backfill(
        store=store,
        bybit_site=args.site,
        work_limit=args.work_limit,
        migrate=args.migrate_postgres,
    )
    text = json.dumps(payload, indent=2, sort_keys=True)
    if args.output is None:
        print(text)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
