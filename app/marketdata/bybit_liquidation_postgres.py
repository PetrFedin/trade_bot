from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.marketdata.bybit_liquidation_forward import (
    BybitLiquidationEvent,
    validate_bybit_public_liquidation_ws_host,
)

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None


@dataclass(frozen=True)
class BybitLiquidationUniverse:
    source_snapshot_id: str
    source_snapshot_observed_at: datetime
    source_host: str
    source_registry_limit: int
    requested_rank_limit: int
    symbols: tuple[str, ...]
    top10_symbols: tuple[str, ...]

    def validate(self) -> None:
        _validate_sha(self.source_snapshot_id, "source snapshot id")
        _utc(self.source_snapshot_observed_at)
        if not self.source_host:
            raise ValueError("liquidation universe source host is required")
        if not 10 <= self.source_registry_limit <= 50:
            raise ValueError("liquidation source registry limit must be within [10, 50]")
        if not 10 <= self.requested_rank_limit <= self.source_registry_limit:
            raise ValueError("liquidation requested rank limit exceeds source registry")
        if not 10 <= len(self.symbols) <= self.requested_rank_limit:
            raise ValueError("liquidation universe must contain at least the current Top-10")
        if len(self.top10_symbols) != 10:
            raise ValueError("liquidation universe must contain exactly ten Top-10 symbols")
        if self.top10_symbols != self.symbols[:10]:
            raise ValueError("liquidation Top-10 must match the first ten ranked symbols")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("liquidation universe symbols must be unique")
        for symbol in self.symbols:
            if symbol != symbol.upper() or not symbol or not symbol.isalnum():
                raise ValueError("liquidation universe symbols must be uppercase alphanumeric")


class PostgresBybitLiquidationStore:
    """Append-only forward liquidation store linked to the live v110 opportunity universe."""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("Bybit liquidation PostgreSQL DSN is required")
        self._dsn = dsn

    @property
    def live_mainnet_order_routing_allowed(self) -> bool:
        return False

    @property
    def order_writes_supported(self) -> bool:
        return False

    def _connect(self):
        if psycopg is None or dict_row is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")
        return psycopg.connect(self._dsn, row_factory=dict_row, autocommit=False)

    def migrate(
        self,
        path: str | Path = "migrations/v116/001_bybit_forward_liquidation_evidence.sql",
    ) -> None:
        sql = Path(path).read_text(encoding="utf-8")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql)
            connection.commit()

    def load_latest_universe(
        self,
        *,
        rank_limit: int = 50,
        now: datetime | None = None,
        maximum_snapshot_age: timedelta = timedelta(minutes=20),
    ) -> BybitLiquidationUniverse:
        if not 10 <= rank_limit <= 50:
            raise ValueError("liquidation rank limit must be within [10, 50]")
        if not timedelta(minutes=1) <= maximum_snapshot_age <= timedelta(hours=2):
            raise ValueError("liquidation source snapshot age bound must be within [1m, 2h]")
        moment = datetime.now(UTC) if now is None else _utc(now)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT snapshot_id, observed_at, host, registry_limit
                    FROM astra_bybit_opportunity_snapshot_v110
                    WHERE top10_complete = true
                    ORDER BY observed_at DESC, snapshot_id DESC
                    LIMIT 1"""
                )
                snapshot = cursor.fetchone()
                if snapshot is None:
                    raise RuntimeError("no complete Bybit v110 opportunity snapshot is available")
                observed_at = _utc(snapshot["observed_at"])
                age = moment - observed_at
                if age < timedelta(minutes=-1):
                    raise RuntimeError("latest Bybit opportunity snapshot is implausibly future-dated")
                if age > maximum_snapshot_age:
                    raise RuntimeError("latest Bybit opportunity snapshot is stale")
                source_registry_limit = int(snapshot["registry_limit"])
                if rank_limit > source_registry_limit:
                    raise RuntimeError(
                        "requested liquidation rank limit exceeds stored opportunity registry"
                    )
                cursor.execute(
                    """SELECT rank, symbol
                    FROM astra_bybit_opportunity_candidate_v110
                    WHERE snapshot_id = %s AND rank <= %s
                    ORDER BY rank ASC""",
                    (snapshot["snapshot_id"], rank_limit),
                )
                rows = cursor.fetchall()
        symbols = tuple(str(row["symbol"]) for row in rows)
        if len(symbols) < 10:
            raise RuntimeError("complete v110 snapshot is missing its ranked Top-10 candidates")
        universe = BybitLiquidationUniverse(
            source_snapshot_id=str(snapshot["snapshot_id"]),
            source_snapshot_observed_at=observed_at,
            source_host=str(snapshot["host"]),
            source_registry_limit=source_registry_limit,
            requested_rank_limit=rank_limit,
            symbols=symbols,
            top10_symbols=symbols[:10],
        )
        universe.validate()
        return universe

    def create_subscription(
        self,
        universe: BybitLiquidationUniverse,
        *,
        ws_host: str,
        started_at: datetime | None = None,
    ) -> str:
        universe.validate()
        host = validate_bybit_public_liquidation_ws_host(ws_host)
        moment = datetime.now(UTC) if started_at is None else _utc(started_at)
        started_at_ms = int(moment.timestamp() * 1000)
        canonical = {
            "source_opportunity_snapshot_id": universe.source_snapshot_id,
            "source_snapshot_observed_at": universe.source_snapshot_observed_at.isoformat(),
            "ws_host": host,
            "rank_limit": universe.requested_rank_limit,
            "symbols": list(universe.symbols),
            "top10_symbols": list(universe.top10_symbols),
            "started_at_ms": started_at_ms,
        }
        subscription_id = _sha(canonical)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_bybit_liquidation_subscription_v116
                        (subscription_id, source_opportunity_snapshot_id,
                         source_snapshot_observed_at, started_at, started_at_ms, ws_host,
                         rank_limit, symbol_count, symbols, top10_symbols, source_schema,
                         stream_topic_schema, forward_only, historical_backfill_available,
                         exchange_event_id_available, research_only, trade_actionable,
                         live_mainnet_order_routing_allowed, created_at)
                        VALUES
                        (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                         'BYBIT_OPPORTUNITY_REGISTRY_V110', 'allLiquidation.{symbol}', true,
                         false, false, true, false, false, %s)
                        ON CONFLICT (subscription_id) DO NOTHING""",
                        (
                            subscription_id,
                            universe.source_snapshot_id,
                            universe.source_snapshot_observed_at,
                            moment,
                            started_at_ms,
                            host,
                            universe.requested_rank_limit,
                            len(universe.symbols),
                            _canonical_json(list(universe.symbols)),
                            _canonical_json(list(universe.top10_symbols)),
                            moment,
                        ),
                    )
        return subscription_id

    def persist_events(
        self,
        subscription_id: str,
        events: tuple[BybitLiquidationEvent, ...],
        *,
        received_at: datetime | None = None,
    ) -> int:
        _validate_sha(subscription_id, "subscription id")
        moment = datetime.now(UTC) if received_at is None else _utc(received_at)
        inserted = 0
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    for event in events:
                        event.validate()
                        event_time = datetime.fromtimestamp(event.event_time_ms / 1000, tz=UTC)
                        bucket_start = datetime.fromtimestamp(event.bucket_start_ms / 1000, tz=UTC)
                        cursor.execute(
                            """INSERT INTO astra_bybit_liquidation_event_v116
                            (event_id, first_subscription_id, system_ts_ms, event_time,
                             event_time_ms, bucket_start, bucket_start_ms, symbol,
                             raw_position_side, liquidated_position_side, quantity_base,
                             bankruptcy_price, estimated_notional_usdt, message_ordinal,
                             dedupe_semantics, exchange_event_id_available,
                             historical_backfill_available, trade_actionable,
                             live_mainnet_order_routing_allowed, received_at)
                            VALUES
                            (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                             'MESSAGE_TS_EVENT_FIELDS_ORDINAL', false, false, false, false, %s)
                            ON CONFLICT (event_id) DO NOTHING""",
                            (
                                event.event_id,
                                subscription_id,
                                event.system_ts_ms,
                                event_time,
                                event.event_time_ms,
                                bucket_start,
                                event.bucket_start_ms,
                                event.symbol,
                                event.raw_position_side,
                                event.liquidated_position_side,
                                event.quantity_base,
                                event.bankruptcy_price,
                                event.estimated_notional_usdt,
                                event.message_ordinal,
                                moment,
                            ),
                        )
                        inserted += max(cursor.rowcount, 0)
        return inserted

    def persist_status(
        self,
        subscription_id: str,
        *,
        state: str,
        connection_epoch: str,
        observed_at_ms: int,
        reason_code: str | None = None,
        created_at: datetime | None = None,
    ) -> str:
        _validate_sha(subscription_id, "subscription id")
        if state not in {"CONNECTING", "CONNECTED", "HEARTBEAT", "DISCONNECTED", "STOPPED"}:
            raise ValueError("invalid Bybit liquidation stream status")
        if len(connection_epoch) != 32 or any(
            char not in "0123456789abcdef" for char in connection_epoch
        ):
            raise ValueError("liquidation connection epoch must be lowercase 32-char hex")
        if isinstance(observed_at_ms, bool) or not isinstance(observed_at_ms, int):
            raise ValueError("liquidation status timestamp must be an integer")
        if observed_at_ms < 0:
            raise ValueError("liquidation status timestamp cannot be negative")
        if reason_code is not None:
            if not 1 <= len(reason_code) <= 80 or not all(
                char.isalnum() or char == "_" for char in reason_code
            ):
                raise ValueError("liquidation status reason code is invalid")
        observed_at = datetime.fromtimestamp(observed_at_ms / 1000, tz=UTC)
        moment = datetime.now(UTC) if created_at is None else _utc(created_at)
        status_id = _sha(
            {
                "subscription_id": subscription_id,
                "state": state,
                "connection_epoch": connection_epoch,
                "observed_at_ms": observed_at_ms,
                "reason_code": reason_code,
            }
        )
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_bybit_liquidation_stream_status_v116
                        (status_id, subscription_id, connection_epoch, observed_at,
                         observed_at_ms, state, reason_code, public_data_only,
                         trade_actionable, live_mainnet_order_routing_allowed, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, true, false, false, %s)
                        ON CONFLICT (status_id) DO NOTHING""",
                        (
                            status_id,
                            subscription_id,
                            connection_epoch,
                            observed_at,
                            observed_at_ms,
                            state,
                            reason_code,
                            moment,
                        ),
                    )
        return status_id


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("liquidation timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _validate_sha(value: str, name: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"liquidation {name} must be lowercase sha256 hex")


__all__ = ["BybitLiquidationUniverse", "PostgresBybitLiquidationStore"]
