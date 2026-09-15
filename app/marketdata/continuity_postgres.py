from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from app.marketdata.continuity import (
    OperationalContinuityCheckpoint,
    _aware,
)
from app.marketdata.operational import OperationalBar, OperationalBarConflict

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None


class PostgresOperationalContinuityStore:
    """Append-only, race-safe single-chain PostgreSQL continuity journal."""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("dsn is required")
        self.dsn = dsn

    def _connect(self):
        if psycopg is None or dict_row is None:
            raise RuntimeError("psycopg is required for PostgreSQL continuity store")
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def migrate(self) -> None:
        migration = (
            Path(__file__).resolve().parents[2]
            / "migrations"
            / "product"
            / "010_operational_market_continuity.sql"
        ).read_text(encoding="utf-8")
        with self._connect() as connection:
            connection.execute(migration)

    def append(self, checkpoint: OperationalContinuityCheckpoint) -> bool:
        checkpoint.validate()
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    self._verify_through_bar(cursor, checkpoint)
                    existing = self._by_id(cursor, checkpoint.checkpoint_id)
                    if existing is not None:
                        self._verify_identical(existing, checkpoint)
                        return False

                    if checkpoint.previous_checkpoint_id is not None:
                        previous = self._lock_previous(cursor, checkpoint)
                        self._verify_previous(previous, checkpoint)
                    occupied = self._chain_slot(cursor, checkpoint)
                    if occupied is not None:
                        if str(occupied["checkpoint_id"]) == checkpoint.checkpoint_id:
                            existing = self._by_id(cursor, checkpoint.checkpoint_id)
                            if existing is None:
                                raise RuntimeError("continuity idempotency lookup failed")
                            self._verify_identical(existing, checkpoint)
                            return False
                        raise ValueError("continuity checkpoint chain fork")

                    cursor.execute(
                        """INSERT INTO astra_operational_market_continuity(
                            checkpoint_id, previous_checkpoint_id, provider, venue,
                            symbol, interval_seconds, through_bar_id,
                            through_close_time, established_at, evidence_source
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING""",
                        (
                            checkpoint.checkpoint_id,
                            checkpoint.previous_checkpoint_id,
                            checkpoint.provider,
                            checkpoint.venue,
                            checkpoint.symbol,
                            checkpoint.interval_seconds,
                            checkpoint.through_bar_id,
                            _aware(checkpoint.through_close_time, "through_close_time"),
                            _aware(checkpoint.established_at, "established_at"),
                            checkpoint.evidence_source,
                        ),
                    )
                    if cursor.rowcount == 1:
                        return True

                    existing = self._by_id(cursor, checkpoint.checkpoint_id)
                    if existing is not None:
                        self._verify_identical(existing, checkpoint)
                        return False
                    occupied = self._chain_slot(cursor, checkpoint)
                    if occupied is not None:
                        raise ValueError("continuity checkpoint chain fork")
                    raise RuntimeError("continuity append lost conflict resolution")

    @staticmethod
    def _by_id(cursor, checkpoint_id: str):
        cursor.execute(
            """SELECT * FROM astra_operational_market_continuity
            WHERE checkpoint_id=%s""",
            (checkpoint_id,),
        )
        return cursor.fetchone()

    @classmethod
    def _verify_identical(
        cls,
        row: Mapping[str, object],
        checkpoint: OperationalContinuityCheckpoint,
    ) -> None:
        stored = cls._checkpoint(row)
        if stored.identity_payload() != checkpoint.identity_payload():
            raise ValueError("continuity checkpoint identity conflict")

    @staticmethod
    def _lock_previous(cursor, checkpoint: OperationalContinuityCheckpoint):
        cursor.execute(
            """SELECT provider, venue, symbol, interval_seconds, through_close_time
            FROM astra_operational_market_continuity
            WHERE checkpoint_id=%s FOR UPDATE""",
            (checkpoint.previous_checkpoint_id,),
        )
        previous = cursor.fetchone()
        if previous is None:
            raise ValueError("continuity previous checkpoint is missing")
        return previous

    @staticmethod
    def _verify_previous(previous, checkpoint: OperationalContinuityCheckpoint) -> None:
        if (
            str(previous["provider"]) != checkpoint.provider
            or str(previous["venue"]) != checkpoint.venue
            or str(previous["symbol"]) != checkpoint.symbol
            or int(str(previous["interval_seconds"])) != checkpoint.interval_seconds
        ):
            raise ValueError("continuity checkpoint stream identity changed")
        previous_close = previous["through_close_time"]
        if not isinstance(previous_close, datetime):
            previous_close = datetime.fromisoformat(str(previous_close))
        if _aware(previous_close, "previous through_close_time") >= _aware(
            checkpoint.through_close_time,
            "through_close_time",
        ):
            raise ValueError("continuity checkpoint did not advance")

    @staticmethod
    def _chain_slot(cursor, checkpoint: OperationalContinuityCheckpoint):
        if checkpoint.previous_checkpoint_id is None:
            cursor.execute(
                """SELECT checkpoint_id FROM astra_operational_market_continuity
                WHERE previous_checkpoint_id IS NULL
                  AND provider=%s AND venue=%s AND symbol=%s AND interval_seconds=%s
                LIMIT 1""",
                (
                    checkpoint.provider,
                    checkpoint.venue,
                    checkpoint.symbol,
                    checkpoint.interval_seconds,
                ),
            )
        else:
            cursor.execute(
                """SELECT checkpoint_id FROM astra_operational_market_continuity
                WHERE previous_checkpoint_id=%s LIMIT 1""",
                (checkpoint.previous_checkpoint_id,),
            )
        return cursor.fetchone()

    @staticmethod
    def _verify_through_bar(cursor, checkpoint: OperationalContinuityCheckpoint) -> None:
        cursor.execute(
            """SELECT provider, venue, symbol, interval_seconds, close_time
            FROM astra_operational_market_bars
            WHERE bar_id=%s FOR SHARE""",
            (checkpoint.through_bar_id,),
        )
        bar = cursor.fetchone()
        if bar is None:
            raise ValueError("continuity through bar is missing")
        close_time = bar["close_time"]
        if not isinstance(close_time, datetime):
            close_time = datetime.fromisoformat(str(close_time))
        if (
            str(bar["provider"]) != checkpoint.provider
            or str(bar["venue"]) != checkpoint.venue
            or str(bar["symbol"]) != checkpoint.symbol
            or int(str(bar["interval_seconds"])) != checkpoint.interval_seconds
            or _aware(close_time, "bar close_time")
            != _aware(checkpoint.through_close_time, "through_close_time")
        ):
            raise ValueError("continuity through bar disagrees with checkpoint")
        cursor.execute(
            """SELECT 1 FROM astra_operational_market_bar_conflicts
            WHERE bar_id=%s LIMIT 1""",
            (checkpoint.through_bar_id,),
        )
        if cursor.fetchone() is not None:
            raise ValueError("continuity through bar is conflicted")

    def latest(
        self,
        *,
        provider: str,
        venue: str,
        symbol: str,
        interval_seconds: int,
    ) -> OperationalContinuityCheckpoint | None:
        for name, value in (
            ("provider", provider),
            ("venue", venue),
            ("symbol", symbol),
        ):
            if not value or value != value.strip().upper():
                raise ValueError(f"{name} must be non-empty normalized uppercase")
        if interval_seconds < 1:
            raise ValueError("interval_seconds must be positive")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT * FROM astra_operational_market_continuity
                    WHERE provider=%s AND venue=%s AND symbol=%s
                      AND interval_seconds=%s
                    ORDER BY through_close_time DESC, checkpoint_id DESC
                    LIMIT 1""",
                    (provider, venue, symbol, interval_seconds),
                )
                row = cursor.fetchone()
        return None if row is None else self._checkpoint(row)

    @staticmethod
    def _checkpoint(row: Mapping[str, object]) -> OperationalContinuityCheckpoint:
        def moment(name: str) -> datetime:
            value = row[name]
            return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))

        checkpoint = OperationalContinuityCheckpoint(
            checkpoint_id=str(row["checkpoint_id"]),
            previous_checkpoint_id=(
                None
                if row["previous_checkpoint_id"] is None
                else str(row["previous_checkpoint_id"])
            ),
            provider=str(row["provider"]),
            venue=str(row["venue"]),
            symbol=str(row["symbol"]),
            interval_seconds=int(str(row["interval_seconds"])),
            through_bar_id=str(row["through_bar_id"]),
            through_close_time=moment("through_close_time"),
            established_at=moment("established_at"),
            evidence_source=str(row["evidence_source"]),
        )
        checkpoint.validate()
        return checkpoint


class PostgresOperationalRepairBarStore:
    """Persist repaired bar economics without scheduling any strategy decision."""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("dsn is required")
        self.dsn = dsn

    def _connect(self):
        if psycopg is None or dict_row is None:
            raise RuntimeError("psycopg is required for PostgreSQL repair store")
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def record_without_decision(
        self,
        bar: OperationalBar,
        *,
        recorded_at: datetime,
    ) -> bool:
        bar.validate()
        if not bar.is_final:
            raise ValueError("OPERATIONAL_BAR_NOT_FINAL")
        moment = _aware(recorded_at, "recorded_at")
        if moment < _aware(bar.received_at, "received_at"):
            raise ValueError("recorded_at cannot precede received_at")
        inserted = False
        conflict = False
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_operational_market_bars(
                            bar_id, provider, venue, symbol, interval_seconds,
                            open_time, close_time, source_timestamp, first_received_at,
                            source_event_id, revision, open_price, high_price, low_price,
                            close_price, volume, content_hash, recorded_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s
                        ) ON CONFLICT (bar_id) DO NOTHING""",
                        self._bar_values(bar, moment),
                    )
                    inserted = cursor.rowcount == 1
                    if not inserted:
                        cursor.execute(
                            """SELECT provider, venue, symbol, interval_seconds,
                                      open_time, close_time, revision,
                                      open_price, high_price, low_price,
                                      close_price, volume, content_hash
                            FROM astra_operational_market_bars
                            WHERE bar_id=%s FOR UPDATE""",
                            (bar.bar_id,),
                        )
                        row = cursor.fetchone()
                        if row is None:
                            raise RuntimeError("repair bar idempotency lookup failed")
                        if (
                            str(row["content_hash"]) != bar.content_hash
                            and not self._same_stored_economics(row, bar)
                        ):
                            cursor.execute(
                                """INSERT INTO astra_operational_market_bar_conflicts(
                                    bar_id, existing_content_hash,
                                    observed_content_hash, observed_payload,
                                    observed_at
                                ) VALUES (%s, %s, %s, %s::jsonb, %s)""",
                                (
                                    bar.bar_id,
                                    str(row["content_hash"]),
                                    bar.content_hash,
                                    json.dumps(
                                        bar.source_payload(),
                                        sort_keys=True,
                                        separators=(",", ":"),
                                    ),
                                    moment,
                                ),
                            )
                            conflict = True
        if conflict:
            raise OperationalBarConflict(f"OPERATIONAL_BAR_CONFLICT:{bar.bar_id}")
        return inserted

    @staticmethod
    def _same_stored_economics(
        row: Mapping[str, object],
        bar: OperationalBar,
    ) -> bool:
        def moment(name: str) -> datetime:
            value = row[name]
            return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))

        return (
            str(row["provider"]) == bar.provider
            and str(row["venue"]) == bar.venue
            and str(row["symbol"]) == bar.symbol
            and int(str(row["interval_seconds"])) == bar.interval_seconds
            and _aware(moment("open_time"), "open_time")
            == _aware(bar.open_time, "open_time")
            and _aware(moment("close_time"), "close_time")
            == _aware(bar.close_time, "close_time")
            and int(str(row["revision"])) == bar.revision
            and Decimal(str(row["open_price"])) == bar.open
            and Decimal(str(row["high_price"])) == bar.high
            and Decimal(str(row["low_price"])) == bar.low
            and Decimal(str(row["close_price"])) == bar.close
            and Decimal(str(row["volume"])) == bar.volume
        )

    @staticmethod
    def _bar_values(bar: OperationalBar, recorded_at: datetime) -> tuple[object, ...]:
        return (
            bar.bar_id,
            bar.provider,
            bar.venue,
            bar.symbol,
            bar.interval_seconds,
            _aware(bar.open_time, "open_time"),
            _aware(bar.close_time, "close_time"),
            _aware(bar.source_timestamp, "source_timestamp"),
            _aware(bar.received_at, "received_at"),
            bar.source_event_id,
            bar.revision,
            bar.open,
            bar.high,
            bar.low,
            bar.close,
            bar.volume,
            bar.content_hash,
            recorded_at,
        )
