from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from app.domain.trading import Bar


class OperationalBarConflict(RuntimeError):
    pass


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _normalized_identity(value: str, name: str) -> str:
    normalized = value.strip().upper()
    if not normalized or normalized != value:
        raise ValueError(f"{name} must be non-empty normalized uppercase")
    return normalized


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


@dataclass(frozen=True)
class OperationalBar:
    """Provider-bound completed bar before conversion to the strategy Bar type."""

    provider: str
    venue: str
    symbol: str
    interval_seconds: int
    open_time: datetime
    close_time: datetime
    source_timestamp: datetime
    received_at: datetime
    source_event_id: str
    is_final: bool
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    revision: int = 0

    def validate(self) -> None:
        _normalized_identity(self.provider, "provider")
        _normalized_identity(self.venue, "venue")
        _normalized_identity(self.symbol, "symbol")
        if self.interval_seconds < 1:
            raise ValueError("interval_seconds must be positive")
        open_time = _aware(self.open_time, "open_time")
        close_time = _aware(self.close_time, "close_time")
        source_time = _aware(self.source_timestamp, "source_timestamp")
        received_at = _aware(self.received_at, "received_at")
        if close_time <= open_time:
            raise ValueError("close_time must follow open_time")
        if close_time - open_time != timedelta(seconds=self.interval_seconds):
            raise ValueError("bar boundaries disagree with interval_seconds")
        if source_time < open_time:
            raise ValueError("source_timestamp cannot precede bar open")
        if received_at < source_time:
            raise ValueError("received_at cannot precede source_timestamp")
        if not self.source_event_id.strip():
            raise ValueError("source_event_id is required")
        if not isinstance(self.is_final, bool):
            raise ValueError("is_final must be boolean")
        if self.revision < 0:
            raise ValueError("revision must be non-negative")
        for name, value in (
            ("open", self.open),
            ("high", self.high),
            ("low", self.low),
            ("close", self.close),
        ):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if not isinstance(self.volume, Decimal) or not self.volume.is_finite() or self.volume < 0:
            raise ValueError("volume must be finite and non-negative")
        if self.high < max(self.open, self.low, self.close):
            raise ValueError("high is below bar prices")
        if self.low > min(self.open, self.high, self.close):
            raise ValueError("low is above bar prices")

    @property
    def bar_id(self) -> str:
        self.validate()
        raw = "|".join(
            (
                self.provider,
                self.venue,
                self.symbol,
                str(self.interval_seconds),
                _aware(self.open_time, "open_time").isoformat(),
                _aware(self.close_time, "close_time").isoformat(),
            )
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    def source_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "provider": self.provider,
            "venue": self.venue,
            "symbol": self.symbol,
            "interval_seconds": self.interval_seconds,
            "open_time": _aware(self.open_time, "open_time").isoformat(),
            "close_time": _aware(self.close_time, "close_time").isoformat(),
            "source_timestamp": _aware(self.source_timestamp, "source_timestamp").isoformat(),
            "source_event_id": self.source_event_id,
            "is_final": self.is_final,
            "open": _decimal_text(self.open),
            "high": _decimal_text(self.high),
            "low": _decimal_text(self.low),
            "close": _decimal_text(self.close),
            "volume": _decimal_text(self.volume),
            "revision": self.revision,
        }

    @property
    def content_hash(self) -> str:
        canonical = json.dumps(
            self.source_payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def strategy_bar(self) -> Bar:
        if not self.is_final:
            raise ValueError("OPERATIONAL_BAR_NOT_FINAL")
        return Bar(
            symbol=self.symbol,
            timestamp=_aware(self.close_time, "close_time"),
            close=self.close,
        )


@dataclass(frozen=True)
class OperationalDecisionTicket:
    ticket_id: str
    strategy_id: str
    bar_id: str
    created_at: datetime

    def validate(self) -> None:
        if not self.ticket_id.strip() or not self.strategy_id.strip() or not self.bar_id.strip():
            raise ValueError("decision ticket identity is required")
        _aware(self.created_at, "created_at")


class OperationalMarketDataStore(Protocol):
    def record_finalized_for_strategy(
        self,
        bar: OperationalBar,
        *,
        strategy_id: str,
        recorded_at: datetime,
    ) -> OperationalDecisionTicket: ...

    def pending_decisions(
        self,
        *,
        strategy_id: str | None = None,
        limit: int = 100,
    ) -> tuple[OperationalDecisionTicket, ...]: ...

    def recent_bars(
        self,
        *,
        provider: str,
        venue: str,
        symbol: str,
        interval_seconds: int,
        through_close_time: datetime,
        limit: int,
    ) -> tuple[OperationalBar, ...]: ...

    def complete_decision(
        self,
        ticket_id: str,
        *,
        outcome_id: str,
        occurred_at: datetime,
    ) -> bool: ...

    def conflict_count(self) -> int: ...


def decision_ticket_id(strategy_id: str, bar_id: str) -> str:
    if not strategy_id.strip() or not bar_id.strip():
        raise ValueError("strategy_id and bar_id are required")
    return hashlib.sha256(f"{strategy_id}|{bar_id}".encode()).hexdigest()


class SQLiteOperationalMarketDataStore:
    """Append-only operational bar truth and restart-safe decision tickets."""

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
                CREATE TABLE IF NOT EXISTS operational_market_bars (
                    bar_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    venue TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    interval_seconds INTEGER NOT NULL,
                    open_time TEXT NOT NULL,
                    close_time TEXT NOT NULL,
                    source_timestamp TEXT NOT NULL,
                    first_received_at TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    open_price TEXT NOT NULL,
                    high_price TEXT NOT NULL,
                    low_price TEXT NOT NULL,
                    close_price TEXT NOT NULL,
                    volume TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_operational_market_bars_window
                ON operational_market_bars(
                    provider, venue, symbol, interval_seconds, close_time, bar_id
                );
                CREATE TABLE IF NOT EXISTS operational_market_bar_conflicts (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    bar_id TEXT NOT NULL,
                    existing_content_hash TEXT NOT NULL,
                    observed_content_hash TEXT NOT NULL,
                    observed_payload TEXT NOT NULL,
                    observed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operational_decision_tickets (
                    ticket_id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    bar_id TEXT NOT NULL REFERENCES operational_market_bars(bar_id),
                    created_at TEXT NOT NULL,
                    UNIQUE(strategy_id, bar_id)
                );
                CREATE INDEX IF NOT EXISTS idx_operational_decision_tickets_strategy
                ON operational_decision_tickets(strategy_id, created_at, ticket_id);
                CREATE TABLE IF NOT EXISTS operational_decision_completions (
                    ticket_id TEXT PRIMARY KEY REFERENCES operational_decision_tickets(ticket_id),
                    outcome_id TEXT NOT NULL,
                    completed_at TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS operational_market_bars_no_update
                BEFORE UPDATE ON operational_market_bars BEGIN
                    SELECT RAISE(ABORT, 'operational_market_bars is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS operational_market_bars_no_delete
                BEFORE DELETE ON operational_market_bars BEGIN
                    SELECT RAISE(ABORT, 'operational_market_bars is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS operational_market_conflicts_no_update
                BEFORE UPDATE ON operational_market_bar_conflicts BEGIN
                    SELECT RAISE(ABORT, 'operational_market_bar_conflicts is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS operational_market_conflicts_no_delete
                BEFORE DELETE ON operational_market_bar_conflicts BEGIN
                    SELECT RAISE(ABORT, 'operational_market_bar_conflicts is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS operational_decision_tickets_no_update
                BEFORE UPDATE ON operational_decision_tickets BEGIN
                    SELECT RAISE(ABORT, 'operational_decision_tickets is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS operational_decision_tickets_no_delete
                BEFORE DELETE ON operational_decision_tickets BEGIN
                    SELECT RAISE(ABORT, 'operational_decision_tickets is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS operational_decision_completions_no_update
                BEFORE UPDATE ON operational_decision_completions BEGIN
                    SELECT RAISE(ABORT, 'operational_decision_completions is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS operational_decision_completions_no_delete
                BEFORE DELETE ON operational_decision_completions BEGIN
                    SELECT RAISE(ABORT, 'operational_decision_completions is append-only');
                END;
                """
            )
        finally:
            connection.close()

    def record_finalized_for_strategy(
        self,
        bar: OperationalBar,
        *,
        strategy_id: str,
        recorded_at: datetime,
    ) -> OperationalDecisionTicket:
        bar.validate()
        if not bar.is_final:
            raise ValueError("OPERATIONAL_BAR_NOT_FINAL")
        if not strategy_id.strip():
            raise ValueError("strategy_id is required")
        moment = _aware(recorded_at, "recorded_at")
        if moment < _aware(bar.received_at, "received_at"):
            raise ValueError("recorded_at cannot precede received_at")
        bar_id = bar.bar_id
        content_hash = bar.content_hash
        ticket_id = decision_ticket_id(strategy_id, bar_id)
        connection = self._connect()
        conflict = False
        ticket: OperationalDecisionTicket | None = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT content_hash FROM operational_market_bars WHERE bar_id=?",
                (bar_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """INSERT INTO operational_market_bars(
                        bar_id, provider, venue, symbol, interval_seconds,
                        open_time, close_time, source_timestamp, first_received_at,
                        source_event_id, revision, open_price, high_price, low_price,
                        close_price, volume, content_hash, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    self._bar_values(bar, moment),
                )
            elif str(row["content_hash"]) != content_hash:
                connection.execute(
                    """INSERT INTO operational_market_bar_conflicts(
                        bar_id, existing_content_hash, observed_content_hash,
                        observed_payload, observed_at
                    ) VALUES (?, ?, ?, ?, ?)""",
                    (
                        bar_id,
                        str(row["content_hash"]),
                        content_hash,
                        json.dumps(bar.source_payload(), sort_keys=True, separators=(",", ":")),
                        moment.isoformat(),
                    ),
                )
                conflict = True
            if not conflict:
                connection.execute(
                    """INSERT OR IGNORE INTO operational_decision_tickets(
                        ticket_id, strategy_id, bar_id, created_at
                    ) VALUES (?, ?, ?, ?)""",
                    (ticket_id, strategy_id, bar_id, moment.isoformat()),
                )
                ticket_row = connection.execute(
                    """SELECT ticket_id, strategy_id, bar_id, created_at
                    FROM operational_decision_tickets WHERE ticket_id=?""",
                    (ticket_id,),
                ).fetchone()
                if ticket_row is None:
                    raise RuntimeError("decision ticket persistence failed")
                ticket = self._ticket(ticket_row)
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
            raise OperationalBarConflict(f"OPERATIONAL_BAR_CONFLICT:{bar_id}")
        if ticket is None:
            raise RuntimeError("decision ticket missing after successful persistence")
        return ticket

    def _bar_values(self, bar: OperationalBar, recorded_at: datetime) -> tuple[object, ...]:
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

    def pending_decisions(
        self,
        *,
        strategy_id: str | None = None,
        limit: int = 100,
    ) -> tuple[OperationalDecisionTicket, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        connection = self._connect()
        try:
            if strategy_id is None:
                rows = connection.execute(
                    """SELECT t.ticket_id, t.strategy_id, t.bar_id, t.created_at
                    FROM operational_decision_tickets t
                    LEFT JOIN operational_decision_completions c USING(ticket_id)
                    WHERE c.ticket_id IS NULL
                    ORDER BY t.created_at, t.ticket_id LIMIT ?""",
                    (limit,),
                ).fetchall()
            else:
                if not strategy_id.strip():
                    raise ValueError("strategy_id cannot be blank")
                rows = connection.execute(
                    """SELECT t.ticket_id, t.strategy_id, t.bar_id, t.created_at
                    FROM operational_decision_tickets t
                    LEFT JOIN operational_decision_completions c USING(ticket_id)
                    WHERE c.ticket_id IS NULL AND t.strategy_id=?
                    ORDER BY t.created_at, t.ticket_id LIMIT ?""",
                    (strategy_id, limit),
                ).fetchall()
        finally:
            connection.close()
        return tuple(self._ticket(row) for row in rows)

    def recent_bars(
        self,
        *,
        provider: str,
        venue: str,
        symbol: str,
        interval_seconds: int,
        through_close_time: datetime,
        limit: int,
    ) -> tuple[OperationalBar, ...]:
        _normalized_identity(provider, "provider")
        _normalized_identity(venue, "venue")
        _normalized_identity(symbol, "symbol")
        if interval_seconds < 1 or limit < 1:
            raise ValueError("interval_seconds and limit must be positive")
        through = _aware(through_close_time, "through_close_time")
        connection = self._connect()
        try:
            rows = connection.execute(
                """SELECT * FROM operational_market_bars
                WHERE provider=? AND venue=? AND symbol=? AND interval_seconds=?
                  AND close_time<=?
                ORDER BY close_time DESC, bar_id DESC LIMIT ?""",
                (provider, venue, symbol, interval_seconds, through.isoformat(), limit),
            ).fetchall()
        finally:
            connection.close()
        return tuple(reversed(tuple(self._bar(row) for row in rows)))

    def complete_decision(
        self,
        ticket_id: str,
        *,
        outcome_id: str,
        occurred_at: datetime,
    ) -> bool:
        if not ticket_id.strip() or not outcome_id.strip():
            raise ValueError("ticket_id and outcome_id are required")
        moment = _aware(occurred_at, "occurred_at")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            ticket = connection.execute(
                "SELECT ticket_id FROM operational_decision_tickets WHERE ticket_id=?",
                (ticket_id,),
            ).fetchone()
            if ticket is None:
                raise KeyError(ticket_id)
            cursor = connection.execute(
                """INSERT OR IGNORE INTO operational_decision_completions(
                    ticket_id, outcome_id, completed_at
                ) VALUES (?, ?, ?)""",
                (ticket_id, outcome_id, moment.isoformat()),
            )
            if cursor.rowcount == 1:
                connection.execute("COMMIT")
                return True
            row = connection.execute(
                "SELECT outcome_id FROM operational_decision_completions WHERE ticket_id=?",
                (ticket_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("decision completion idempotency lookup failed")
            if str(row["outcome_id"]) != outcome_id:
                raise ValueError("OPERATIONAL_DECISION_COMPLETION_CONFLICT")
            connection.execute("COMMIT")
            return False
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        finally:
            connection.close()

    def conflict_count(self) -> int:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM operational_market_bar_conflicts"
            ).fetchone()
        finally:
            connection.close()
        return 0 if row is None else int(row["count"])

    @staticmethod
    def _ticket(row: sqlite3.Row) -> OperationalDecisionTicket:
        ticket = OperationalDecisionTicket(
            ticket_id=str(row["ticket_id"]),
            strategy_id=str(row["strategy_id"]),
            bar_id=str(row["bar_id"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )
        ticket.validate()
        return ticket

    @staticmethod
    def _bar(row: sqlite3.Row) -> OperationalBar:
        bar = OperationalBar(
            provider=str(row["provider"]),
            venue=str(row["venue"]),
            symbol=str(row["symbol"]),
            interval_seconds=int(row["interval_seconds"]),
            open_time=datetime.fromisoformat(str(row["open_time"])),
            close_time=datetime.fromisoformat(str(row["close_time"])),
            source_timestamp=datetime.fromisoformat(str(row["source_timestamp"])),
            received_at=datetime.fromisoformat(str(row["first_received_at"])),
            source_event_id=str(row["source_event_id"]),
            is_final=True,
            open=Decimal(str(row["open_price"])),
            high=Decimal(str(row["high_price"])),
            low=Decimal(str(row["low_price"])),
            close=Decimal(str(row["close_price"])),
            volume=Decimal(str(row["volume"])),
            revision=int(row["revision"]),
        )
        bar.validate()
        return bar
