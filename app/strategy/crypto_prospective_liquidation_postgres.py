from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.strategy.crypto_prospective_liquidation_context import (
    LiquidationPoint,
    LiquidationStatusPoint,
    ProspectiveLiquidationContext,
    assess_single_subscription_coverage,
    build_prospective_liquidation_context,
)

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None


class PostgresProspectiveLiquidationContextStore:
    """Build and persist immutable pre-signal liquidation context for v112 seeds."""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("prospective liquidation PostgreSQL DSN is required")
        self._dsn = dsn

    @property
    def order_writes_supported(self) -> bool:
        return False

    @property
    def live_mainnet_order_routing_allowed(self) -> bool:
        return False

    def _connect(self):
        if psycopg is None or dict_row is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")
        return psycopg.connect(self._dsn, row_factory=dict_row, autocommit=False)

    def migrate(
        self,
        path: str | Path = "migrations/v117/001_bybit_prospective_liquidation_context.sql",
    ) -> None:
        sql = Path(path).read_text(encoding="utf-8")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql)
            connection.commit()

    def attach_pending(
        self,
        *,
        evaluated_at: datetime | None = None,
        limit: int = 100,
        minimum_signal_age_seconds: int = 120,
        maximum_status_age_seconds: int = 60,
    ) -> tuple[ProspectiveLiquidationContext, ...]:
        if isinstance(limit, bool) or not 1 <= limit <= 10_000:
            raise ValueError("prospective liquidation attach limit must be within [1, 10000]")
        if not 60 <= minimum_signal_age_seconds <= 3600:
            raise ValueError("prospective liquidation signal age must be within [60, 3600]")
        if not 20 <= maximum_status_age_seconds <= 300:
            raise ValueError("prospective liquidation status age must be within [20, 300]")
        moment = datetime.now(UTC) if evaluated_at is None else _utc(evaluated_at)
        cutoff = moment - timedelta(seconds=minimum_signal_age_seconds)
        seed_ids = self._load_pending_seed_ids(cutoff=cutoff, limit=limit)
        attached: list[ProspectiveLiquidationContext] = []
        for seed_id in seed_ids:
            context = self.build_for_seed(
                seed_id,
                evaluated_at=moment,
                maximum_status_age_seconds=maximum_status_age_seconds,
            )
            self.persist(context)
            attached.append(context)
        return tuple(attached)

    def build_for_seed(
        self,
        seed_id: str,
        *,
        evaluated_at: datetime,
        maximum_status_age_seconds: int = 60,
    ) -> ProspectiveLiquidationContext:
        _validate_sha(seed_id, "shadow seed")
        moment = _utc(evaluated_at)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT seed_id, source_snapshot_id, symbol, side, signal_available_at
                    FROM astra_bybit_shadow_seed_v112
                    WHERE seed_id = %s""",
                    (seed_id,),
                )
                seed = cursor.fetchone()
                if seed is None:
                    raise RuntimeError("prospective liquidation seed does not exist")
                signal = _utc(seed["signal_available_at"])
                if moment < signal + timedelta(seconds=60):
                    raise RuntimeError("prospective liquidation seed is too recent to attach")
                coverage_start = signal - timedelta(minutes=60)
                candidates = self._load_subscription_candidates(
                    cursor,
                    symbol=str(seed["symbol"]),
                    coverage_start=coverage_start,
                )
                selected_subscription_id: str | None = None
                selected_statuses: tuple[LiquidationStatusPoint, ...] = ()
                best_unqualified: tuple[
                    int,
                    str,
                    tuple[LiquidationStatusPoint, ...],
                ] | None = None
                for subscription_id in candidates:
                    statuses = self._load_statuses(
                        cursor,
                        subscription_id=subscription_id,
                        coverage_start=coverage_start,
                        signal_available_at=signal,
                        maximum_status_age_seconds=maximum_status_age_seconds,
                    )
                    qualified, reasons, _start_at, _end_at = (
                        assess_single_subscription_coverage(
                            window_start=coverage_start,
                            signal_available_at=signal,
                            statuses=statuses,
                            maximum_status_age_seconds=maximum_status_age_seconds,
                        )
                    )
                    if qualified:
                        selected_subscription_id = subscription_id
                        selected_statuses = statuses
                        break
                    score = len(reasons)
                    if best_unqualified is None or score < best_unqualified[0]:
                        best_unqualified = (score, subscription_id, statuses)
                if selected_subscription_id is None and best_unqualified is not None:
                    selected_subscription_id = best_unqualified[1]
                    selected_statuses = best_unqualified[2]
                events = self._load_events(
                    cursor,
                    symbol=str(seed["symbol"]),
                    coverage_start=coverage_start,
                    signal_available_at=signal,
                )
        return build_prospective_liquidation_context(
            seed_id=str(seed["seed_id"]),
            source_snapshot_id=str(seed["source_snapshot_id"]),
            symbol=str(seed["symbol"]),
            side=str(seed["side"]),
            signal_available_at=signal,
            evaluated_at=moment,
            coverage_subscription_id=selected_subscription_id,
            coverage_statuses=selected_statuses,
            events=events,
            maximum_status_age_seconds=maximum_status_age_seconds,
        )

    def persist(self, context: ProspectiveLiquidationContext) -> str:
        context.validate()
        payload = context.to_payload()
        context_id = context.context_id
        with self._connect() as connection:
            try:
                with connection.transaction():
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """SELECT context_id, context_json
                            FROM astra_bybit_shadow_liquidation_context_v117
                            WHERE seed_id = %s
                            FOR UPDATE""",
                            (context.seed_id,),
                        )
                        existing = cursor.fetchone()
                        if existing is not None:
                            if str(existing["context_id"]) != context_id:
                                raise RuntimeError(
                                    "prospective liquidation seed already has divergent context"
                                )
                            if existing["context_json"] != payload:
                                raise RuntimeError(
                                    "prospective liquidation context payload diverged"
                                )
                            return context_id
                        cursor.execute(
                            """INSERT INTO astra_bybit_shadow_liquidation_context_v117
                            (context_id, seed_id, source_snapshot_id, symbol, side,
                             signal_available_at, coverage_window_start_at,
                             coverage_subscription_id, coverage_qualified,
                             coverage_reason_codes, coverage_start_status_at,
                             coverage_end_status_at, maximum_status_age_seconds, evaluated_at,
                             context_json, prospective,
                             liquidation_feature_used_for_source_ranking,
                             parameter_retuning_performed, trade_actionable,
                             strategy_promotion_allowed, demo_activation_allowed,
                             live_activation_allowed, bybit_live_order_routing_allowed,
                             created_at)
                            VALUES
                            (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s,
                             %s, %s, %s::jsonb, true, false, false, false, false,
                             false, false, false, %s)""",
                            (
                                context_id,
                                context.seed_id,
                                context.source_snapshot_id,
                                context.symbol,
                                context.side,
                                context.signal_available_at,
                                context.coverage_window_start_at,
                                context.coverage_subscription_id,
                                context.coverage_qualified,
                                _json(list(context.coverage_reason_codes)),
                                context.coverage_start_status_at,
                                context.coverage_end_status_at,
                                context.maximum_status_age_seconds,
                                context.evaluated_at,
                                _json(payload),
                                context.evaluated_at,
                            ),
                        )
                        for window in context.windows:
                            cursor.execute(
                                """INSERT INTO astra_bybit_shadow_liquidation_window_v117
                                (context_id, window_minutes, window_start_at, window_end_at,
                                 event_count, long_liquidation_count,
                                 short_liquidation_count, long_estimated_notional_usdt,
                                 short_estimated_notional_usdt,
                                 total_estimated_notional_usdt,
                                 long_minus_short_estimated_notional_usdt,
                                 normalized_long_minus_short_imbalance,
                                 largest_event_estimated_notional_usdt,
                                 first_event_at, last_event_at, known_zero)
                                VALUES
                                (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                 %s, %s, %s, %s)""",
                                (
                                    context_id,
                                    window.window_minutes,
                                    window.window_start_at,
                                    window.window_end_at,
                                    window.event_count,
                                    window.long_liquidation_count,
                                    window.short_liquidation_count,
                                    window.long_estimated_notional_usdt,
                                    window.short_estimated_notional_usdt,
                                    window.total_estimated_notional_usdt,
                                    window.long_minus_short_estimated_notional_usdt,
                                    window.normalized_long_minus_short_imbalance,
                                    window.largest_event_estimated_notional_usdt,
                                    window.first_event_at,
                                    window.last_event_at,
                                    window.known_zero,
                                ),
                            )
            except Exception:
                connection.rollback()
                raise
        return context_id

    def _load_pending_seed_ids(
        self,
        *,
        cutoff: datetime,
        limit: int,
    ) -> tuple[str, ...]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT seed.seed_id
                    FROM astra_bybit_shadow_seed_v112 AS seed
                    LEFT JOIN astra_bybit_shadow_liquidation_context_v117 AS context
                      ON context.seed_id = seed.seed_id
                    WHERE seed.signal_available_at <= %s
                      AND context.seed_id IS NULL
                    ORDER BY seed.signal_available_at, seed.seed_id
                    LIMIT %s""",
                    (cutoff, limit),
                )
                rows = cursor.fetchall()
        return tuple(str(row["seed_id"]) for row in rows)

    @staticmethod
    def _load_subscription_candidates(
        cursor: Any,
        *,
        symbol: str,
        coverage_start: datetime,
    ) -> tuple[str, ...]:
        cursor.execute(
            """SELECT subscription_id
            FROM astra_bybit_liquidation_subscription_v116
            WHERE started_at <= %s
              AND symbols ? %s
            ORDER BY started_at DESC, subscription_id DESC
            LIMIT 50""",
            (coverage_start, symbol),
        )
        return tuple(str(row["subscription_id"]) for row in cursor.fetchall())

    @staticmethod
    def _load_statuses(
        cursor: Any,
        *,
        subscription_id: str,
        coverage_start: datetime,
        signal_available_at: datetime,
        maximum_status_age_seconds: int,
    ) -> tuple[LiquidationStatusPoint, ...]:
        lower_bound = coverage_start - timedelta(seconds=maximum_status_age_seconds)
        cursor.execute(
            """SELECT observed_at, state
            FROM astra_bybit_liquidation_stream_status_v116
            WHERE subscription_id = %s
              AND observed_at >= %s
              AND observed_at <= %s
            ORDER BY observed_at, status_id""",
            (subscription_id, lower_bound, signal_available_at),
        )
        return tuple(
            LiquidationStatusPoint(
                observed_at=_utc(row["observed_at"]),
                state=str(row["state"]),
            )
            for row in cursor.fetchall()
        )

    @staticmethod
    def _load_events(
        cursor: Any,
        *,
        symbol: str,
        coverage_start: datetime,
        signal_available_at: datetime,
    ) -> tuple[LiquidationPoint, ...]:
        cursor.execute(
            """SELECT event_id, event_time, liquidated_position_side,
                      estimated_notional_usdt
            FROM astra_bybit_liquidation_event_v116
            WHERE symbol = %s
              AND event_time >= %s
              AND event_time < %s
            ORDER BY event_time, event_id""",
            (symbol, coverage_start, signal_available_at),
        )
        return tuple(
            LiquidationPoint(
                event_id=str(row["event_id"]),
                event_time=_utc(row["event_time"]),
                liquidated_position_side=str(row["liquidated_position_side"]),
                estimated_notional_usdt=Decimal(
                    str(row["estimated_notional_usdt"])
                ),
            )
            for row in cursor.fetchall()
        )


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("prospective liquidation timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _validate_sha(value: str, name: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{name} must be lowercase sha256 hex")


__all__ = ["PostgresProspectiveLiquidationContextStore"]
