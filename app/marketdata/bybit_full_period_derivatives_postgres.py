from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.marketdata.bybit_derivatives_history import (
    BybitAccountRatioPoint,
    BybitHistoricalFundingPoint,
    BybitOpenInterestPoint,
)
from app.marketdata.bybit_full_period_derivatives import (
    ACCOUNT_RATIO,
    DERIVATIVES_SOURCES,
    FUNDING,
    OPEN_INTEREST,
    BybitDerivativesSourceDayAudit,
)

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None


@dataclass(frozen=True)
class BybitFullPeriodDerivativesStoredCoverage:
    completed_by_source_symbol: dict[str, dict[str, tuple[date, ...]]]
    unavailable_retry_after_by_source_symbol: dict[
        str, dict[str, dict[date, datetime]]
    ]


class PostgresBybitFullPeriodDerivativesStore:
    """Append-only authoritative store for public Bybit derivatives history."""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("full-period derivatives PostgreSQL DSN is required")
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
        path: str | Path = "migrations/v114/001_bybit_full_period_derivatives.sql",
    ) -> None:
        sql = Path(path).read_text(encoding="utf-8")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql)
            connection.commit()

    def coverage_state(
        self,
        symbols: Sequence[str],
    ) -> BybitFullPeriodDerivativesStoredCoverage:
        normalized = _validate_symbols(symbols)
        completed = {
            source: {symbol: [] for symbol in normalized}
            for source in DERIVATIVES_SOURCES
        }
        unavailable = {
            source: {symbol: {} for symbol in normalized}
            for source in DERIVATIVES_SOURCES
        }
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT source_series, symbol, archive_date
                    FROM astra_bybit_derivatives_day_v114
                    WHERE state='COMPLETE' AND symbol = ANY(%s)
                    ORDER BY source_series, symbol, archive_date""",
                    (list(normalized),),
                )
                for row in cursor.fetchall():
                    source = _validate_source_value(row["source_series"])
                    completed[source][str(row["symbol"])].append(row["archive_date"])
                cursor.execute(
                    """SELECT DISTINCT ON (
                            attempt.source_series, attempt.symbol, attempt.archive_date
                        )
                        attempt.source_series, attempt.symbol, attempt.archive_date,
                        attempt.retry_after
                    FROM astra_bybit_derivatives_day_v114 AS attempt
                    WHERE attempt.state='UNAVAILABLE'
                      AND attempt.symbol = ANY(%s)
                      AND NOT EXISTS (
                          SELECT 1
                          FROM astra_bybit_derivatives_day_v114 AS complete
                          WHERE complete.source_series=attempt.source_series
                            AND complete.symbol=attempt.symbol
                            AND complete.archive_date=attempt.archive_date
                            AND complete.state='COMPLETE'
                      )
                    ORDER BY attempt.source_series, attempt.symbol, attempt.archive_date,
                             attempt.observed_at DESC, attempt.attempt_id DESC""",
                    (list(normalized),),
                )
                for row in cursor.fetchall():
                    source = _validate_source_value(row["source_series"])
                    retry_after = row["retry_after"]
                    if not isinstance(retry_after, datetime):
                        raise ValueError("stored derivatives retry timestamp is invalid")
                    unavailable[source][str(row["symbol"])][row["archive_date"]] = _utc(
                        retry_after
                    )
        return BybitFullPeriodDerivativesStoredCoverage(
            completed_by_source_symbol={
                source: {
                    symbol: tuple(values) for symbol, values in by_symbol.items()
                }
                for source, by_symbol in completed.items()
            },
            unavailable_retry_after_by_source_symbol=unavailable,
        )

    def persist_complete_day(
        self,
        *,
        audit: BybitDerivativesSourceDayAudit,
        points: Sequence[
            BybitOpenInterestPoint | BybitAccountRatioPoint | BybitHistoricalFundingPoint
        ],
        observed_at: datetime,
        created_at: datetime | None = None,
    ) -> str:
        audit.validate()
        if not audit.complete:
            raise ValueError("cannot persist incomplete derivatives source day as complete")
        observed = _utc(observed_at)
        created = datetime.now(UTC) if created_at is None else _utc(created_at)
        payloads = tuple(_point_payload(audit.source, point) for point in points)
        if len(payloads) != audit.actual_point_count:
            raise ValueError("derivatives source-day point count does not match audit")
        if any(payload["symbol"] != audit.symbol for payload in payloads):
            raise ValueError("derivatives source-day point symbol does not match audit")
        fingerprint = _sha(payloads)
        attempt_payload = {
            "source": audit.source,
            "symbol": audit.symbol,
            "archive_date": audit.archive_date.isoformat(),
            "query_start_at": audit.query_start_at,
            "query_end_at": audit.query_end_at,
            "point_fingerprint": fingerprint,
        }
        attempt_id = _sha(attempt_payload)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    for point, payload in zip(points, payloads, strict=True):
                        self._persist_point(
                            cursor,
                            source=audit.source,
                            point=point,
                            payload=payload,
                            created_at=created,
                        )
                    cursor.execute(
                        """INSERT INTO astra_bybit_derivatives_day_v114
                        (attempt_id, source_series, symbol, archive_date, query_start_at,
                         query_end_at, state, error_code, retry_after, point_count,
                         expected_point_count, missing_point_count, extra_point_count,
                         exact_grid_required, query_window_complete, point_fingerprint,
                         observed_at, trade_actionable, demo_activation_allowed,
                         live_activation_allowed, bybit_live_order_routing_allowed, created_at)
                        VALUES
                        (%s, %s, %s, %s, %s, %s, 'COMPLETE', NULL, NULL, %s, %s,
                         0, 0, %s, true, %s, %s, false, false, false, false, %s)
                        ON CONFLICT (source_series, symbol, archive_date)
                        WHERE state='COMPLETE' DO NOTHING""",
                        (
                            attempt_id,
                            audit.source,
                            audit.symbol,
                            audit.archive_date,
                            _parse_time(audit.query_start_at),
                            _parse_time(audit.query_end_at),
                            audit.actual_point_count,
                            audit.expected_point_count,
                            audit.exact_grid_required,
                            fingerprint,
                            observed,
                            created,
                        ),
                    )
                    if cursor.rowcount == 0:
                        cursor.execute(
                            """SELECT attempt_id, query_start_at, query_end_at, point_count,
                                      expected_point_count, exact_grid_required,
                                      point_fingerprint
                            FROM astra_bybit_derivatives_day_v114
                            WHERE source_series=%s AND symbol=%s AND archive_date=%s
                              AND state='COMPLETE'""",
                            (audit.source, audit.symbol, audit.archive_date),
                        )
                        row = cursor.fetchone()
                        if row is None:
                            raise RuntimeError("derivatives complete-day lookup lost row")
                        if (
                            row["attempt_id"] != attempt_id
                            or _utc(row["query_start_at"]) != _parse_time(audit.query_start_at)
                            or _utc(row["query_end_at"]) != _parse_time(audit.query_end_at)
                            or int(row["point_count"]) != audit.actual_point_count
                            or _optional_int(row["expected_point_count"])
                            != audit.expected_point_count
                            or bool(row["exact_grid_required"]) != audit.exact_grid_required
                            or row["point_fingerprint"] != fingerprint
                        ):
                            raise ValueError(
                                "derivatives complete day conflicts with stored immutable history"
                            )
        return attempt_id

    def persist_unavailable(
        self,
        *,
        source: str,
        symbol: str,
        archive_date: date,
        query_start_at: datetime,
        query_end_at: datetime,
        error_code: str,
        retry_after: datetime,
        observed_at: datetime,
        created_at: datetime | None = None,
    ) -> str:
        _validate_source_value(source)
        _validate_symbols((symbol,))
        query_start = _utc(query_start_at)
        query_end = _utc(query_end_at)
        observed = _utc(observed_at)
        retry = _utc(retry_after)
        if query_start.date() != archive_date or query_end <= query_start:
            raise ValueError("derivatives unavailable query interval is invalid")
        if not error_code.strip() or error_code != error_code.strip():
            raise ValueError("derivatives unavailable error code is invalid")
        if retry <= observed:
            raise ValueError("derivatives retry_after must be after observed_at")
        created = datetime.now(UTC) if created_at is None else _utc(created_at)
        attempt_id = _sha(
            {
                "state": "UNAVAILABLE",
                "source": source,
                "symbol": symbol,
                "archive_date": archive_date.isoformat(),
                "query_start_at": query_start.isoformat(),
                "query_end_at": query_end.isoformat(),
                "error_code": error_code,
                "retry_after": retry.isoformat(),
                "observed_at": observed.isoformat(),
            }
        )
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_bybit_derivatives_day_v114
                        (attempt_id, source_series, symbol, archive_date, query_start_at,
                         query_end_at, state, error_code, retry_after, point_count,
                         expected_point_count, missing_point_count, extra_point_count,
                         exact_grid_required, query_window_complete, point_fingerprint,
                         observed_at, trade_actionable, demo_activation_allowed,
                         live_activation_allowed, bybit_live_order_routing_allowed, created_at)
                        VALUES
                        (%s, %s, %s, %s, %s, %s, 'UNAVAILABLE', %s, %s, NULL, NULL,
                         NULL, NULL, NULL, NULL, NULL, %s, false, false, false, false, %s)
                        ON CONFLICT (attempt_id) DO NOTHING""",
                        (
                            attempt_id,
                            source,
                            symbol,
                            archive_date,
                            query_start,
                            query_end,
                            error_code,
                            retry,
                            observed,
                            created,
                        ),
                    )
        return attempt_id

    def load_open_interest(
        self,
        *,
        symbol: str,
        start_at: datetime,
        end_at: datetime,
    ) -> tuple[BybitOpenInterestPoint, ...]:
        rows = self._load_rows(
            table="astra_bybit_open_interest_v114",
            fields="symbol, timestamp_at, open_interest, single_open_interest",
            symbol=symbol,
            start_at=start_at,
            end_at=end_at,
        )
        result = tuple(
            BybitOpenInterestPoint(
                symbol=str(row["symbol"]),
                timestamp_ms=_timestamp_ms(row["timestamp_at"]),
                open_interest=_decimal(row["open_interest"]),
                single_open_interest=(
                    None
                    if row["single_open_interest"] is None
                    else _decimal(row["single_open_interest"])
                ),
            )
            for row in rows
        )
        for point in result:
            point.validate()
        return result

    def load_account_ratio(
        self,
        *,
        symbol: str,
        start_at: datetime,
        end_at: datetime,
    ) -> tuple[BybitAccountRatioPoint, ...]:
        rows = self._load_rows(
            table="astra_bybit_account_ratio_v114",
            fields="symbol, timestamp_at, buy_ratio, sell_ratio",
            symbol=symbol,
            start_at=start_at,
            end_at=end_at,
        )
        result = tuple(
            BybitAccountRatioPoint(
                symbol=str(row["symbol"]),
                timestamp_ms=_timestamp_ms(row["timestamp_at"]),
                buy_ratio=_decimal(row["buy_ratio"]),
                sell_ratio=_decimal(row["sell_ratio"]),
            )
            for row in rows
        )
        for point in result:
            point.validate()
        return result

    def load_funding(
        self,
        *,
        symbol: str,
        start_at: datetime,
        end_at: datetime,
    ) -> tuple[BybitHistoricalFundingPoint, ...]:
        rows = self._load_rows(
            table="astra_bybit_funding_rate_v114",
            fields="symbol, timestamp_at, funding_rate",
            symbol=symbol,
            start_at=start_at,
            end_at=end_at,
        )
        result = tuple(
            BybitHistoricalFundingPoint(
                symbol=str(row["symbol"]),
                timestamp_ms=_timestamp_ms(row["timestamp_at"]),
                funding_rate=_decimal(row["funding_rate"]),
            )
            for row in rows
        )
        for point in result:
            point.validate()
        return result

    def _load_rows(
        self,
        *,
        table: str,
        fields: str,
        symbol: str,
        start_at: datetime,
        end_at: datetime,
    ) -> list[Mapping[str, Any]]:
        if table not in {
            "astra_bybit_open_interest_v114",
            "astra_bybit_account_ratio_v114",
            "astra_bybit_funding_rate_v114",
        }:
            raise ValueError("derivatives load table is not allowlisted")
        _validate_symbols((symbol,))
        start = _utc(start_at)
        end = _utc(end_at)
        if end <= start:
            raise ValueError("derivatives load interval is invalid")
        query = (
            f"SELECT {fields} FROM {table} "  # noqa: S608 - table/fields are internal allowlists
            "WHERE symbol=%s AND timestamp_at >= %s AND timestamp_at < %s "
            "ORDER BY timestamp_at"
        )
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, (symbol, start, end))
                return list(cursor.fetchall())

    def _persist_point(
        self,
        cursor: Any,
        *,
        source: str,
        point: BybitOpenInterestPoint | BybitAccountRatioPoint | BybitHistoricalFundingPoint,
        payload: Mapping[str, str | None],
        created_at: datetime,
    ) -> None:
        point_id = _sha(payload)
        timestamp_at = datetime.fromtimestamp(point.timestamp_ms / 1000, tz=UTC)
        if source == OPEN_INTEREST and isinstance(point, BybitOpenInterestPoint):
            cursor.execute(
                """INSERT INTO astra_bybit_open_interest_v114
                (point_id, symbol, timestamp_at, open_interest, single_open_interest,
                 trade_actionable, bybit_live_order_routing_allowed, created_at)
                VALUES (%s, %s, %s, %s, %s, false, false, %s)
                ON CONFLICT (symbol, timestamp_at) DO NOTHING""",
                (
                    point_id,
                    point.symbol,
                    timestamp_at,
                    point.open_interest,
                    point.single_open_interest,
                    created_at,
                ),
            )
            table = "astra_bybit_open_interest_v114"
            fields = "point_id, open_interest, single_open_interest"
        elif source == ACCOUNT_RATIO and isinstance(point, BybitAccountRatioPoint):
            cursor.execute(
                """INSERT INTO astra_bybit_account_ratio_v114
                (point_id, symbol, timestamp_at, buy_ratio, sell_ratio,
                 trade_actionable, bybit_live_order_routing_allowed, created_at)
                VALUES (%s, %s, %s, %s, %s, false, false, %s)
                ON CONFLICT (symbol, timestamp_at) DO NOTHING""",
                (
                    point_id,
                    point.symbol,
                    timestamp_at,
                    point.buy_ratio,
                    point.sell_ratio,
                    created_at,
                ),
            )
            table = "astra_bybit_account_ratio_v114"
            fields = "point_id, buy_ratio, sell_ratio"
        elif source == FUNDING and isinstance(point, BybitHistoricalFundingPoint):
            cursor.execute(
                """INSERT INTO astra_bybit_funding_rate_v114
                (point_id, symbol, timestamp_at, funding_rate,
                 trade_actionable, bybit_live_order_routing_allowed, created_at)
                VALUES (%s, %s, %s, %s, false, false, %s)
                ON CONFLICT (symbol, timestamp_at) DO NOTHING""",
                (
                    point_id,
                    point.symbol,
                    timestamp_at,
                    point.funding_rate,
                    created_at,
                ),
            )
            table = "astra_bybit_funding_rate_v114"
            fields = "point_id, funding_rate"
        else:
            raise ValueError("derivatives point type does not match source")
        if cursor.rowcount != 0:
            return
        query = (
            f"SELECT {fields} FROM {table} "  # noqa: S608 - internal fixed table/field set
            "WHERE symbol=%s AND timestamp_at=%s"
        )
        cursor.execute(query, (point.symbol, timestamp_at))
        row = cursor.fetchone()
        if row is None or row["point_id"] != point_id:
            raise ValueError("derivatives point conflicts with stored immutable history")


def _point_payload(
    source: str,
    point: BybitOpenInterestPoint | BybitAccountRatioPoint | BybitHistoricalFundingPoint,
) -> dict[str, str | None]:
    point.validate()
    base: dict[str, str | None] = {
        "source": source,
        "symbol": point.symbol,
        "timestamp_at": datetime.fromtimestamp(
            point.timestamp_ms / 1000, tz=UTC
        ).isoformat(),
    }
    if source == OPEN_INTEREST and isinstance(point, BybitOpenInterestPoint):
        base["open_interest"] = str(point.open_interest)
        base["single_open_interest"] = (
            None if point.single_open_interest is None else str(point.single_open_interest)
        )
    elif source == ACCOUNT_RATIO and isinstance(point, BybitAccountRatioPoint):
        base["buy_ratio"] = str(point.buy_ratio)
        base["sell_ratio"] = str(point.sell_ratio)
    elif source == FUNDING and isinstance(point, BybitHistoricalFundingPoint):
        base["funding_rate"] = str(point.funding_rate)
    else:
        raise ValueError("derivatives point type does not match source")
    return base


def _validate_symbols(symbols: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(symbols)
    if not normalized or tuple(sorted(set(normalized))) != normalized:
        raise ValueError("derivatives store symbols must be sorted and unique")
    if any(
        symbol != symbol.strip().upper()
        or not symbol.endswith("USDT")
        or not symbol.isalnum()
        for symbol in normalized
    ):
        raise ValueError("derivatives store symbols must be normalized USDT")
    return normalized


def _validate_source_value(value: Any) -> str:
    if not isinstance(value, str) or value not in DERIVATIVES_SOURCES:
        raise ValueError("stored derivatives source is invalid")
    return value


def _sha(value: Any) -> str:
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("derivatives store timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _parse_time(value: str) -> datetime:
    return _utc(datetime.fromisoformat(value))


def _timestamp_ms(value: Any) -> int:
    if not isinstance(value, datetime):
        raise ValueError("stored derivatives timestamp is invalid")
    return int(_utc(value).timestamp() * 1000)


def _decimal(value: Any) -> Decimal:
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise ValueError("stored derivatives decimal is not finite")
    return parsed


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("stored derivatives integer is invalid")
    return int(value)
