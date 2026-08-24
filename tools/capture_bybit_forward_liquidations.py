from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime, timedelta

from app.marketdata.bybit_liquidation_forward import (
    capture_bybit_public_liquidations,
)
from app.marketdata.bybit_liquidation_postgres import (
    PostgresBybitLiquidationStore,
)

_DEFAULT_DSN_ENV = "BYBIT_OPPORTUNITY_DATABASE_DSN"


async def run_forward_liquidation_capture(
    *,
    dsn: str,
    ws_host: str = "stream.bybit.com",
    rank_limit: int = 50,
    maximum_snapshot_age: timedelta | None = None,
    run_seconds: float | None = None,
    migrate: bool = False,
) -> dict[str, object]:
    if not dsn.strip():
        raise ValueError("Bybit liquidation database DSN is required")
    if run_seconds is not None and not 1 <= run_seconds <= 86_400:
        raise ValueError(
            "liquidation bounded runtime must be within [1, 86400] seconds"
        )
    active_maximum_age = (
        timedelta(minutes=20)
        if maximum_snapshot_age is None
        else maximum_snapshot_age
    )
    store = PostgresBybitLiquidationStore(dsn)
    if migrate:
        await asyncio.to_thread(store.migrate)
    now = datetime.now(UTC)
    universe = await asyncio.to_thread(
        store.load_latest_universe,
        rank_limit=rank_limit,
        now=now,
        maximum_snapshot_age=active_maximum_age,
    )
    subscription_id = await asyncio.to_thread(
        store.create_subscription,
        universe,
        ws_host=ws_host,
        started_at=now,
    )
    stop_event = asyncio.Event()
    timer: asyncio.TimerHandle | None = None
    if run_seconds is not None:
        timer = asyncio.get_running_loop().call_later(
            run_seconds,
            stop_event.set,
        )

    async def on_events(events) -> None:
        await asyncio.to_thread(
            store.persist_events,
            subscription_id,
            events,
        )

    async def on_status(
        state: str,
        connection_epoch: str,
        observed_at_ms: int,
        reason_code: str | None,
    ) -> None:
        await asyncio.to_thread(
            store.persist_status,
            subscription_id,
            state=state,
            connection_epoch=connection_epoch,
            observed_at_ms=observed_at_ms,
            reason_code=reason_code,
        )

    initial: dict[str, object] = {
        "schema": "BYBIT_FORWARD_LIQUIDATION_CAPTURE_V116",
        "subscription_id": subscription_id,
        "source_opportunity_snapshot_id": universe.source_snapshot_id,
        "source_snapshot_observed_at": (
            universe.source_snapshot_observed_at.isoformat()
        ),
        "source_host": universe.source_host,
        "ws_host": ws_host,
        "rank_limit": rank_limit,
        "symbols": list(universe.symbols),
        "top10_symbols": list(universe.top10_symbols),
        "forward_only": True,
        "historical_backfill_available": False,
        "exchange_event_id_available": False,
        "trade_actionable": False,
        "bybit_live_order_routing_allowed": False,
    }
    print(
        "BYBIT_LIQUIDATION_CAPTURE_START="
        + json.dumps(initial, sort_keys=True)
    )
    try:
        await capture_bybit_public_liquidations(
            universe.symbols,
            on_events=on_events,
            on_status=on_status,
            host=ws_host,
            stop_event=stop_event,
        )
    finally:
        if timer is not None:
            timer.cancel()
    return initial


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Capture Bybit public all-liquidation events for the latest immutable "
            "Top-10/Top-50 opportunity snapshot. This command has no authenticated "
            "or order-write surface."
        )
    )
    parser.add_argument("--ws-host", default="stream.bybit.com")
    parser.add_argument("--rank-limit", type=int, default=50)
    parser.add_argument("--source-max-age-minutes", type=int, default=20)
    parser.add_argument("--run-seconds", type=float)
    parser.add_argument("--migrate-postgres", action="store_true")
    parser.add_argument("--database-dsn-env", default=_DEFAULT_DSN_ENV)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if not 1 <= args.source_max_age_minutes <= 120:
        raise SystemExit(
            "--source-max-age-minutes must be within [1, 120]"
        )
    dsn = os.environ.get(args.database_dsn_env, "")
    if not dsn.strip():
        raise SystemExit(
            "required PostgreSQL DSN environment variable is missing:"
            + args.database_dsn_env
        )
    try:
        asyncio.run(
            run_forward_liquidation_capture(
                dsn=dsn,
                ws_host=args.ws_host,
                rank_limit=args.rank_limit,
                maximum_snapshot_age=timedelta(
                    minutes=args.source_max_age_minutes
                ),
                run_seconds=args.run_seconds,
                migrate=args.migrate_postgres,
            )
        )
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
