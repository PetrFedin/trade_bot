from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.marketdata.bybit_opportunity_registry import BybitOpportunitySnapshot

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None


class PostgresBybitOpportunityStore:
    """Append-only PostgreSQL store for public Bybit opportunity snapshots."""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("Bybit opportunity PostgreSQL DSN is required")
        if psycopg is None:
            raise RuntimeError("install the postgresql extra to use opportunity persistence")
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
        path: str | Path = "migrations/v110/001_bybit_opportunity_registry.sql",
    ) -> None:
        sql = Path(path).read_text(encoding="utf-8")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql)
            connection.commit()

    def persist(
        self,
        snapshot: BybitOpportunitySnapshot,
        *,
        created_at: datetime | None = None,
    ) -> str:
        snapshot.validate()
        moment = datetime.now(UTC) if created_at is None else _utc(created_at)
        observed_at = datetime.fromtimestamp(snapshot.observed_at_ms / 1000, tz=UTC)
        payload = snapshot.to_payload()
        snapshot_id = snapshot.snapshot_id
        payload_json = _canonical_json(payload)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_bybit_opportunity_snapshot_v110
                        (snapshot_id, observed_at, observed_at_ms, host, registry_limit,
                         eligible_symbol_count, source_instrument_count, source_ticker_count,
                         top10_complete, top10_symbols, registry_population_complete, blockers,
                         excluded_reasons, snapshot_json, research_only, trade_actionable,
                         strategy_promotion_allowed, live_activation_allowed,
                         bybit_live_order_routing_allowed, created_at)
                        VALUES
                        (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb,
                         %s::jsonb, %s::jsonb, true, false, false, false, false, %s)
                        ON CONFLICT (host, observed_at_ms) DO NOTHING""",
                        (
                            snapshot_id,
                            observed_at,
                            snapshot.observed_at_ms,
                            snapshot.host,
                            snapshot.registry_limit,
                            snapshot.eligible_symbol_count,
                            snapshot.source_instrument_count,
                            snapshot.source_ticker_count,
                            snapshot.top10_complete,
                            _canonical_json(list(snapshot.top10_symbols)),
                            snapshot.registry_population_complete,
                            _canonical_json(list(snapshot.blockers)),
                            _canonical_json(
                                {
                                    symbol: list(reasons)
                                    for symbol, reasons in sorted(
                                        snapshot.excluded_reasons.items()
                                    )
                                }
                            ),
                            payload_json,
                            moment,
                        ),
                    )
                    inserted = cursor.rowcount == 1
                    if not inserted:
                        existing = self._load_identity(
                            cursor,
                            host=snapshot.host,
                            observed_at_ms=snapshot.observed_at_ms,
                        )
                        if existing["snapshot_id"] != snapshot_id:
                            raise ValueError(
                                "Bybit opportunity snapshot timestamp already contains "
                                "different canonical content"
                            )
                        if _canonical_json(existing["snapshot_json"]) != payload_json:
                            raise ValueError(
                                "Bybit opportunity snapshot idempotency payload mismatch"
                            )
                        return snapshot_id
                    for candidate in snapshot.candidates:
                        cursor.execute(
                            """INSERT INTO astra_bybit_opportunity_candidate_v110
                            (snapshot_id, rank, symbol, is_top10, universe_score, listing_days,
                             turnover_24h_usdt, open_interest_value_usdt, spread_bps,
                             funding_rate, price_24h_fraction, turnover_percentile,
                             open_interest_percentile, spread_quality_percentile,
                             history_percentile, rank_drivers, signal_side, trade_actionable,
                             strategy_promotion_allowed, live_activation_allowed,
                             bybit_live_order_routing_allowed)
                            VALUES
                            (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                             %s, %s::jsonb, 'UNASSIGNED', false, false, false, false)""",
                            (
                                snapshot_id,
                                candidate.rank,
                                candidate.symbol,
                                candidate.is_top10,
                                candidate.universe_score,
                                candidate.listing_days,
                                candidate.turnover_24h_usdt,
                                candidate.open_interest_value_usdt,
                                candidate.spread_bps,
                                candidate.funding_rate,
                                candidate.price_24h_fraction,
                                candidate.turnover_percentile,
                                candidate.open_interest_percentile,
                                candidate.spread_quality_percentile,
                                candidate.history_percentile,
                                _canonical_json(list(candidate.rank_drivers)),
                            ),
                        )
        return snapshot_id

    def latest_snapshot_payload(self, *, host: str | None = None) -> Mapping[str, Any] | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                if host is None:
                    cursor.execute(
                        """SELECT snapshot_json
                        FROM astra_bybit_opportunity_snapshot_v110
                        ORDER BY observed_at DESC, snapshot_id DESC
                        LIMIT 1"""
                    )
                else:
                    cursor.execute(
                        """SELECT snapshot_json
                        FROM astra_bybit_opportunity_snapshot_v110
                        WHERE host=%s
                        ORDER BY observed_at DESC, snapshot_id DESC
                        LIMIT 1""",
                        (host,),
                    )
                row = cursor.fetchone()
                if row is None:
                    return None
                payload = row["snapshot_json"]
                if not isinstance(payload, Mapping):
                    raise ValueError("stored Bybit opportunity snapshot_json is not an object")
                return dict(payload)

    @staticmethod
    def _load_identity(cursor, *, host: str, observed_at_ms: int) -> Mapping[str, Any]:
        cursor.execute(
            """SELECT snapshot_id, snapshot_json
            FROM astra_bybit_opportunity_snapshot_v110
            WHERE host=%s AND observed_at_ms=%s""",
            (host, observed_at_ms),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("Bybit opportunity idempotency lookup lost existing snapshot")
        return row


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Bybit opportunity timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
