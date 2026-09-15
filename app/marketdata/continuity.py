from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from app.marketdata.operational import OperationalBar, OperationalBarConflict


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _identity(value: str, name: str) -> str:
    if not value or value != value.strip().upper():
        raise ValueError(f"{name} must be non-empty normalized uppercase")
    return value


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


@dataclass(frozen=True)
class OperationalContinuityCheckpoint:
    checkpoint_id: str
    previous_checkpoint_id: str | None
    provider: str
    venue: str
    symbol: str
    interval_seconds: int
    through_bar_id: str
    through_close_time: datetime
    established_at: datetime
    evidence_source: str

    def validate(self) -> None:
        if not self.checkpoint_id.strip():
            raise ValueError("checkpoint_id is required")
        if self.previous_checkpoint_id is not None and not self.previous_checkpoint_id.strip():
            raise ValueError("previous_checkpoint_id cannot be blank")
        _identity(self.provider, "provider")
        _identity(self.venue, "venue")
        _identity(self.symbol, "symbol")
        if self.interval_seconds < 1:
            raise ValueError("interval_seconds must be positive")
        if not self.through_bar_id.strip():
            raise ValueError("through_bar_id is required")
        through = _aware(self.through_close_time, "through_close_time")
        established = _aware(self.established_at, "established_at")
        if established < through:
            raise ValueError("established_at cannot precede through_close_time")
        if not self.evidence_source.strip():
            raise ValueError("evidence_source is required")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "previous_checkpoint_id": self.previous_checkpoint_id,
            "provider": self.provider,
            "venue": self.venue,
            "symbol": self.symbol,
            "interval_seconds": self.interval_seconds,
            "through_bar_id": self.through_bar_id,
            "through_close_time": _aware(
                self.through_close_time, "through_close_time"
            ).isoformat(),
            "established_at": _aware(self.established_at, "established_at").isoformat(),
            "evidence_source": self.evidence_source,
        }


def continuity_checkpoint_id(
    *,
    previous_checkpoint_id: str | None,
    provider: str,
    venue: str,
    symbol: str,
    interval_seconds: int,
    through_bar_id: str,
    through_close_time: datetime,
    evidence_source: str,
) -> str:
    _identity(provider, "provider")
    _identity(venue, "venue")
    _identity(symbol, "symbol")
    if interval_seconds < 1 or not through_bar_id.strip() or not evidence_source.strip():
        raise ValueError("continuity checkpoint identity is incomplete")
    payload = {
        "previous_checkpoint_id": previous_checkpoint_id,
        "provider": provider,
        "venue": venue,
        "symbol": symbol,
        "interval_seconds": interval_seconds,
        "through_bar_id": through_bar_id,
        "through_close_time": _aware(
            through_close_time, "through_close_time"
        ).isoformat(),
        "evidence_source": evidence_source,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


class OperationalContinuityStore(Protocol):
    def append(self, checkpoint: OperationalContinuityCheckpoint) -> bool: ...

    def latest(
        self,
        *,
        provider: str,
        venue: str,
        symbol: str,
        interval_seconds: int,
    ) -> OperationalContinuityCheckpoint | None: ...


class OperationalRepairBarStore(Protocol):
    def record_without_decision(
        self,
        bar: OperationalBar,
        *,
        recorded_at: datetime,
    ) -> bool: ...


class SQLiteOperationalContinuityStore:
    """Append-only continuity high-water journal in the operational market-data DB."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        if self.path != ":memory:":
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS operational_market_continuity (
                    checkpoint_id TEXT PRIMARY KEY,
                    previous_checkpoint_id TEXT,
                    provider TEXT NOT NULL,
                    venue TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    interval_seconds INTEGER NOT NULL,
                    through_bar_id TEXT NOT NULL,
                    through_close_time TEXT NOT NULL,
                    established_at TEXT NOT NULL,
                    evidence_source TEXT NOT NULL,
                    FOREIGN KEY(previous_checkpoint_id)
                        REFERENCES operational_market_continuity(checkpoint_id)
                );
                CREATE INDEX IF NOT EXISTS idx_operational_market_continuity_latest
                ON operational_market_continuity(
                    provider, venue, symbol, interval_seconds,
                    through_close_time DESC, checkpoint_id DESC
                );
                CREATE TRIGGER IF NOT EXISTS operational_market_continuity_no_update
                BEFORE UPDATE ON operational_market_continuity BEGIN
                    SELECT RAISE(ABORT, 'operational_market_continuity is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS operational_market_continuity_no_delete
                BEFORE DELETE ON operational_market_continuity BEGIN
                    SELECT RAISE(ABORT, 'operational_market_continuity is append-only');
                END;
                """
            )
        finally:
            connection.close()

    def append(self, checkpoint: OperationalContinuityCheckpoint) -> bool:
        checkpoint.validate()
        canonical = json.dumps(
            checkpoint.payload(),
            sort_keys=True,
            separators=(",", ":"),
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if checkpoint.previous_checkpoint_id is not None:
                previous = connection.execute(
                    """SELECT provider, venue, symbol, interval_seconds,
                              through_close_time
                       FROM operational_market_continuity
                       WHERE checkpoint_id=?""",
                    (checkpoint.previous_checkpoint_id,),
                ).fetchone()
                if previous is None:
                    raise ValueError("continuity previous checkpoint is missing")
                if (
                    str(previous["provider"]) != checkpoint.provider
                    or str(previous["venue"]) != checkpoint.venue
                    or str(previous["symbol"]) != checkpoint.symbol
                    or int(previous["interval_seconds"]) != checkpoint.interval_seconds
                ):
                    raise ValueError("continuity checkpoint stream identity changed")
                if datetime.fromisoformat(str(previous["through_close_time"])) >= _aware(
                    checkpoint.through_close_time,
                    "through_close_time",
                ):
                    raise ValueError("continuity checkpoint did not advance")
            existing = connection.execute(
                "SELECT * FROM operational_market_continuity WHERE checkpoint_id=?",
                (checkpoint.checkpoint_id,),
            ).fetchone()
            if existing is not None:
                existing_payload = json.dumps(
                    self._checkpoint(existing).payload(),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if existing_payload != canonical:
                    raise ValueError("continuity checkpoint identity conflict")
                connection.execute("COMMIT")
                return False
            connection.execute(
                """INSERT INTO operational_market_continuity(
                    checkpoint_id, previous_checkpoint_id, provider, venue, symbol,
                    interval_seconds, through_bar_id, through_close_time,
                    established_at, evidence_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    checkpoint.checkpoint_id,
                    checkpoint.previous_checkpoint_id,
                    checkpoint.provider,
                    checkpoint.venue,
                    checkpoint.symbol,
                    checkpoint.interval_seconds,
                    checkpoint.through_bar_id,
                    _aware(
                        checkpoint.through_close_time,
                        "through_close_time",
                    ).isoformat(),
                    _aware(checkpoint.established_at, "established_at").isoformat(),
                    checkpoint.evidence_source,
                ),
            )
            connection.execute("COMMIT")
            return True
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        finally:
            connection.close()

    def latest(
        self,
        *,
        provider: str,
        venue: str,
        symbol: str,
        interval_seconds: int,
    ) -> OperationalContinuityCheckpoint | None:
        _identity(provider, "provider")
        _identity(venue, "venue")
        _identity(symbol, "symbol")
        if interval_seconds < 1:
            raise ValueError("interval_seconds must be positive")
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT * FROM operational_market_continuity
                   WHERE provider=? AND venue=? AND symbol=? AND interval_seconds=?
                   ORDER BY through_close_time DESC, checkpoint_id DESC
                   LIMIT 1""",
                (provider, venue, symbol, interval_seconds),
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else self._checkpoint(row)

    @staticmethod
    def _checkpoint(row: sqlite3.Row) -> OperationalContinuityCheckpoint:
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
            interval_seconds=int(row["interval_seconds"]),
            through_bar_id=str(row["through_bar_id"]),
            through_close_time=datetime.fromisoformat(str(row["through_close_time"])),
            established_at=datetime.fromisoformat(str(row["established_at"])),
            evidence_source=str(row["evidence_source"]),
        )
        checkpoint.validate()
        return checkpoint


class SQLiteOperationalRepairBarStore:
    """Persist repaired bar truth without ever scheduling a strategy decision."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        if self.path != ":memory:":
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
        return connection

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
        connection = self._connect()
        conflict = False
        inserted = False
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT content_hash FROM operational_market_bars WHERE bar_id=?",
                (bar.bar_id,),
            ).fetchone()
            if row is None:
                cursor = connection.execute(
                    """INSERT INTO operational_market_bars(
                        bar_id, provider, venue, symbol, interval_seconds,
                        open_time, close_time, source_timestamp, first_received_at,
                        source_event_id, revision, open_price, high_price, low_price,
                        close_price, volume, content_hash, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    self._bar_values(bar, moment),
                )
                inserted = cursor.rowcount == 1
            elif str(row["content_hash"]) != bar.content_hash:
                connection.execute(
                    """INSERT INTO operational_market_bar_conflicts(
                        bar_id, existing_content_hash, observed_content_hash,
                        observed_payload, observed_at
                    ) VALUES (?, ?, ?, ?, ?)""",
                    (
                        bar.bar_id,
                        str(row["content_hash"]),
                        bar.content_hash,
                        json.dumps(
                            bar.source_payload(),
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        moment.isoformat(),
                    ),
                )
                conflict = True
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        finally:
            connection.close()
        if conflict:
            raise OperationalBarConflict(f"OPERATIONAL_BAR_CONFLICT:{bar.bar_id}")
        return inserted

    @staticmethod
    def _bar_values(bar: OperationalBar, recorded_at: datetime) -> tuple[object, ...]:
        return (
            bar.bar_id,
            bar.provider,
            bar.venue,
            bar.symbol,
            bar.interval_seconds,
            _aware(bar.open_time, "open_time").isoformat(),
            _aware(bar.close_time, "close_time").isoformat(),
            _aware(bar.source_timestamp, "source_timestamp").isoformat(),
            _aware(bar.received_at, "received_at").isoformat(),
            bar.source_event_id,
            bar.revision,
            _decimal_text(bar.open),
            _decimal_text(bar.high),
            _decimal_text(bar.low),
            _decimal_text(bar.close),
            _decimal_text(bar.volume),
            bar.content_hash,
            recorded_at.isoformat(),
        )
