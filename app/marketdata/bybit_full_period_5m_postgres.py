from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.marketdata.bybit_public_archive import BybitArchiveAcquisition, archive_url
from app.marketdata.bybit_v5 import BybitKlineBar

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None


@dataclass(frozen=True)
class BybitFullPeriod5mStoredCoverage:
    completed_by_symbol: dict[str, tuple[date, ...]]
    unavailable_retry_after_by_symbol: dict[str, dict[date, datetime]]


class PostgresBybitFullPeriod5mStore:
    """Append-only authoritative store for official Bybit archive-derived 5m bars."""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("full-period 5m PostgreSQL DSN is required")
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
        path: str | Path = "migrations/v113/001_bybit_full_period_5m.sql",
    ) -> None:
        sql = Path(path).read_text(encoding="utf-8")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql)
            connection.commit()

    def coverage_state(
        self,
        symbols: Sequence[str],
    ) -> BybitFullPeriod5mStoredCoverage:
        normalized = _validate_symbols(symbols)
        completed: dict[str, list[date]] = {symbol: [] for symbol in normalized}
        unavailable: dict[str, dict[date, datetime]] = {
            symbol: {} for symbol in normalized
        }
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT symbol, archive_date
                    FROM astra_bybit_5m_archive_day_v113
                    WHERE state='COMPLETE' AND symbol = ANY(%s)
                    ORDER BY symbol, archive_date""",
                    (list(normalized),),
                )
                for row in cursor.fetchall():
                    completed[str(row["symbol"])].append(row["archive_date"])
                cursor.execute(
                    """SELECT DISTINCT ON (attempt.symbol, attempt.archive_date)
                        attempt.symbol, attempt.archive_date, attempt.retry_after
                    FROM astra_bybit_5m_archive_day_v113 AS attempt
                    WHERE attempt.state='UNAVAILABLE'
                      AND attempt.symbol = ANY(%s)
                      AND NOT EXISTS (
                          SELECT 1
                          FROM astra_bybit_5m_archive_day_v113 AS complete
                          WHERE complete.symbol=attempt.symbol
                            AND complete.archive_date=attempt.archive_date
                            AND complete.state='COMPLETE'
                      )
                    ORDER BY attempt.symbol, attempt.archive_date, attempt.observed_at DESC,
                             attempt.attempt_id DESC""",
                    (list(normalized),),
                )
                for row in cursor.fetchall():
                    retry_after = row["retry_after"]
                    if not isinstance(retry_after, datetime):
                        raise ValueError("stored full-period 5m retry timestamp is invalid")
                    unavailable[str(row["symbol"])][row["archive_date"]] = _utc(
                        retry_after
                    )
        return BybitFullPeriod5mStoredCoverage(
            completed_by_symbol={
                symbol: tuple(values) for symbol, values in completed.items()
            },
            unavailable_retry_after_by_symbol=unavailable,
        )

    def persist_complete_day(
        self,
        *,
        symbol: str,
        archive_date: date,
        acquisition: BybitArchiveAcquisition,
        observed_at: datetime,
        created_at: datetime | None = None,
    ) -> str:
        _validate_symbols((symbol,))
        observed = _utc(observed_at)
        created = datetime.now(UTC) if created_at is None else _utc(created_at)
        acquisition.validate(requested_symbols=(symbol,), minimum_bars=1)
        bars = acquisition.klines.bars
        if not bars or any(bar.symbol != symbol for bar in bars):
            raise ValueError("full-period 5m acquisition symbol mismatch")
        if any(bar.start_time.astimezone(UTC).date() != archive_date for bar in bars):
            raise ValueError("full-period 5m acquisition crosses archive day boundary")
        _validate_five_minute_bars(bars)
        fingerprint = _bars_fingerprint(bars)
        attempt_id = _sha(
            {
                "state": "COMPLETE",
                "symbol": symbol,
                "archive_date": archive_date.isoformat(),
                "bar_fingerprint": fingerprint,
            }
        )
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    for bar in bars:
                        self._persist_bar(cursor, bar, archive_date=archive_date, created_at=created)
                    cursor.execute(
                        """INSERT INTO astra_bybit_5m_archive_day_v113
                        (attempt_id, symbol, archive_date, source_url, state, error_code,
                         retry_after, bar_count, first_bar_at, last_bar_at, bar_fingerprint,
                         observed_at, trade_actionable, demo_activation_allowed,
                         live_activation_allowed, bybit_live_order_routing_allowed, created_at)
                        VALUES
                        (%s, %s, %s, %s, 'COMPLETE', NULL, NULL, %s, %s, %s, %s,
                         %s, false, false, false, false, %s)
                        ON CONFLICT (symbol, archive_date) WHERE state='COMPLETE'
                        DO NOTHING""",
                        (
                            attempt_id,
                            symbol,
                            archive_date,
                            archive_url(symbol, archive_date),
                            len(bars),
                            bars[0].start_time,
                            bars[-1].start_time,
                            fingerprint,
                            observed,
                            created,
                        ),
                    )
                    if cursor.rowcount == 0:
                        cursor.execute(
                            """SELECT attempt_id, bar_count, first_bar_at, last_bar_at,
                                      bar_fingerprint
                            FROM astra_bybit_5m_archive_day_v113
                            WHERE symbol=%s AND archive_date=%s AND state='COMPLETE'""",
                            (symbol, archive_date),
                        )
                        row = cursor.fetchone()
                        if row is None:
                            raise RuntimeError("full-period 5m complete-day lookup lost row")
                        if (
                            row["attempt_id"] != attempt_id
                            or int(row["bar_count"]) != len(bars)
                            or _utc(row["first_bar_at"]) != _utc(bars[0].start_time)
                            or _utc(row["last_bar_at"]) != _utc(bars[-1].start_time)
                            or row["bar_fingerprint"] != fingerprint
                        ):
                            raise ValueError(
                                "full-period 5m archive day conflicts with stored history"
                            )
        return attempt_id

    def persist_unavailable(
        self,
        *,
        symbol: str,
        archive_date: date,
        error_code: str,
        retry_after: datetime,
        observed_at: datetime,
        created_at: datetime | None = None,
    ) -> str:
        _validate_symbols((symbol,))
        if not error_code.strip() or error_code != error_code.strip():
            raise ValueError("full-period 5m unavailable error code is invalid")
        retry = _utc(retry_after)
        observed = _utc(observed_at)
        if retry <= observed:
            raise ValueError("full-period 5m retry_after must be after observed_at")
        created = datetime.now(UTC) if created_at is None else _utc(created_at)
        attempt_id = _sha(
            {
                "state": "UNAVAILABLE",
                "symbol": symbol,
                "archive_date": archive_date.isoformat(),
                "error_code": error_code,
                "retry_after": retry.isoformat(),
                "observed_at": observed.isoformat(),
            }
        )
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_bybit_5m_archive_day_v113
                        (attempt_id, symbol, archive_date, source_url, state, error_code,
                         retry_after, bar_count, first_bar_at, last_bar_at, bar_fingerprint,
                         observed_at, trade_actionable, demo_activation_allowed,
                         live_activation_allowed, bybit_live_order_routing_allowed, created_at)
                        VALUES
                        (%s, %s, %s, %s, 'UNAVAILABLE', %s, %s, NULL, NULL, NULL, NULL,
                         %s, false, false, false, false, %s)
                        ON CONFLICT (attempt_id) DO NOTHING""",
                        (
                            attempt_id,
                            symbol,
                            archive_date,
                            archive_url(symbol, archive_date),
                            error_code,
                            retry,
                            observed,
                            created,
                        ),
                    )
        return attempt_id

    def load_bars(
        self,
        *,
        symbols: Sequence[str],
        start_at: datetime | None = None,
        end_at: datetime | None = None,
    ) -> tuple[BybitKlineBar, ...]:
        normalized = _validate_symbols(symbols)
        start = None if start_at is None else _utc(start_at)
        end = None if end_at is None else _utc(end_at)
        if start is not None and end is not None and end <= start:
            raise ValueError("full-period 5m load interval is invalid")
        clauses = ["symbol = ANY(%s)"]
        params: list[Any] = [list(normalized)]
        if start is not None:
            clauses.append("start_time >= %s")
            params.append(start)
        if end is not None:
            clauses.append("start_time < %s")
            params.append(end)
        query = (
            "SELECT symbol, start_time, open, high, low, close, volume, turnover "
            "FROM astra_bybit_5m_bar_v113 WHERE "
            + " AND ".join(clauses)
            + " ORDER BY symbol, start_time"
        )
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, tuple(params))
                rows = cursor.fetchall()
        bars = tuple(
            BybitKlineBar(
                symbol=str(row["symbol"]),
                start_time=_utc(row["start_time"]),
                open=_decimal(row["open"]),
                high=_decimal(row["high"]),
                low=_decimal(row["low"]),
                close=_decimal(row["close"]),
                volume=_decimal(row["volume"]),
                turnover=_decimal(row["turnover"]),
            )
            for row in rows
        )
        for bar in bars:
            bar.validate()
        return bars

    def _persist_bar(
        self,
        cursor: Any,
        bar: BybitKlineBar,
        *,
        archive_date: date,
        created_at: datetime,
    ) -> None:
        bar.validate()
        payload = _bar_payload(bar, archive_date=archive_date)
        bar_id = _sha(payload)
        cursor.execute(
            """INSERT INTO astra_bybit_5m_bar_v113
            (bar_id, symbol, start_time, archive_date, open, high, low, close, volume,
             turnover, trade_actionable, demo_activation_allowed, live_activation_allowed,
             bybit_live_order_routing_allowed, created_at)
            VALUES
            (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, false, false, false, false, %s)
            ON CONFLICT (symbol, start_time) DO NOTHING""",
            (
                bar_id,
                bar.symbol,
                bar.start_time,
                archive_date,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                bar.turnover,
                created_at,
            ),
        )
        if cursor.rowcount != 0:
            return
        cursor.execute(
            """SELECT bar_id, archive_date, open, high, low, close, volume, turnover
            FROM astra_bybit_5m_bar_v113
            WHERE symbol=%s AND start_time=%s""",
            (bar.symbol, bar.start_time),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("full-period 5m bar idempotency lookup lost row")
        stored_payload = {
            "symbol": bar.symbol,
            "start_time": _utc(bar.start_time).isoformat(),
            "archive_date": row["archive_date"].isoformat(),
            "open": str(_decimal(row["open"])),
            "high": str(_decimal(row["high"])),
            "low": str(_decimal(row["low"])),
            "close": str(_decimal(row["close"])),
            "volume": str(_decimal(row["volume"])),
            "turnover": str(_decimal(row["turnover"])),
        }
        if row["bar_id"] != bar_id or stored_payload != payload:
            raise ValueError("full-period 5m bar conflicts with stored immutable history")


def _validate_symbols(symbols: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(symbols)
    if not normalized or tuple(sorted(set(normalized))) != normalized:
        raise ValueError("full-period 5m symbols must be sorted and unique")
    if any(
        symbol != symbol.strip().upper()
        or not symbol.endswith("USDT")
        or not symbol.isalnum()
        for symbol in normalized
    ):
        raise ValueError("full-period 5m symbols must be normalized USDT symbols")
    return normalized


def _validate_five_minute_bars(bars: Sequence[BybitKlineBar]) -> None:
    previous: datetime | None = None
    for bar in bars:
        bar.validate()
        moment = _utc(bar.start_time)
        if moment.second != 0 or moment.microsecond != 0 or moment.minute % 5 != 0:
            raise ValueError("full-period archive bar is not aligned to 5 minutes")
        if previous is not None and moment <= previous:
            raise ValueError("full-period archive bars must be strictly chronological")
        previous = moment


def _bars_fingerprint(bars: Sequence[BybitKlineBar]) -> str:
    payload = [
        _bar_payload(bar, archive_date=_utc(bar.start_time).date()) for bar in bars
    ]
    return _sha(payload)


def _bar_payload(bar: BybitKlineBar, *, archive_date: date) -> dict[str, str]:
    return {
        "symbol": bar.symbol,
        "start_time": _utc(bar.start_time).isoformat(),
        "archive_date": archive_date.isoformat(),
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": str(bar.volume),
        "turnover": str(bar.turnover),
    }


def _sha(value: Any) -> str:
    canonical = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _decimal(value: Any) -> Decimal:
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise ValueError("full-period 5m stored numeric value must be finite")
    return parsed


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("full-period 5m timestamp must be timezone-aware")
    return value.astimezone(UTC)
