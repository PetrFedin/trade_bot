from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from app.domain.trading import OrderIntent, Side
from app.oms.reconciliation import (
    BrokerPortfolioTruth,
    PortfolioReconciliationResult,
    reconcile_portfolio,
)
from app.oms.store import OrderRecord
from app.portfolio.ledger import PortfolioLedger

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None


@dataclass(frozen=True)
class AccountReconciliationTruth:
    sequence: int
    event_id: str
    matched: bool
    cash_delta: Decimal
    position_mismatches: int
    reasons: tuple[str, ...]
    broker_cash: Decimal
    portfolio_fingerprint: str
    observed_at: datetime


class AccountReconciliationBlocked(RuntimeError):
    def __init__(self, reasons: tuple[str, ...]) -> None:
        normalized = tuple(sorted(set(reasons)))
        if not normalized:
            raise ValueError("account reconciliation block requires at least one reason")
        self.reasons = normalized
        super().__init__(f"ACCOUNT_RECONCILIATION_BLOCKED:{','.join(normalized)}")


class AccountReconciliationTruthStore(Protocol):
    def append(
        self,
        *,
        result: PortfolioReconciliationResult,
        broker_cash: Decimal,
        portfolio_fingerprint: str,
        observed_at: datetime,
    ) -> AccountReconciliationTruth: ...

    def current(self) -> AccountReconciliationTruth | None: ...


def _aware_utc(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def portfolio_financial_fingerprint(ledger: PortfolioLedger) -> str:
    material = json.dumps(
        {
            "cash": str(ledger.cash),
            "positions": [
                {"symbol": position.symbol, "quantity": str(position.quantity)}
                for position in ledger.positions()
                if position.quantity != 0
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _event_id(
    *,
    result: PortfolioReconciliationResult,
    broker_cash: Decimal,
    portfolio_fingerprint: str,
    observed_at: datetime,
) -> str:
    material = json.dumps(
        {
            "matched": result.matched,
            "cash_delta": str(result.cash_delta),
            "position_deltas": [
                [symbol, str(delta)] for symbol, delta in result.position_deltas
            ],
            "reasons": list(result.reasons),
            "broker_cash": str(broker_cash),
            "portfolio_fingerprint": portfolio_fingerprint,
            "observed_at": observed_at.isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class SQLiteAccountReconciliationTruthStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        if self.path != ":memory:":
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_account_reconciliation_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    matched INTEGER NOT NULL CHECK (matched IN (0, 1)),
                    cash_delta TEXT NOT NULL,
                    position_mismatches INTEGER NOT NULL CHECK (position_mismatches >= 0),
                    reasons TEXT NOT NULL,
                    broker_cash TEXT NOT NULL,
                    portfolio_fingerprint TEXT NOT NULL,
                    observed_at TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS paper_account_reconciliation_events_no_update
                BEFORE UPDATE ON paper_account_reconciliation_events
                BEGIN
                    SELECT RAISE(ABORT, 'paper_account_reconciliation_events is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS paper_account_reconciliation_events_no_delete
                BEFORE DELETE ON paper_account_reconciliation_events
                BEGIN
                    SELECT RAISE(ABORT, 'paper_account_reconciliation_events is append-only');
                END;
                """
            )
        finally:
            connection.close()

    @staticmethod
    def _row(row: sqlite3.Row) -> AccountReconciliationTruth:
        return AccountReconciliationTruth(
            sequence=int(row["sequence"]),
            event_id=str(row["event_id"]),
            matched=bool(row["matched"]),
            cash_delta=Decimal(str(row["cash_delta"])),
            position_mismatches=int(row["position_mismatches"]),
            reasons=tuple(json.loads(str(row["reasons"]))),
            broker_cash=Decimal(str(row["broker_cash"])),
            portfolio_fingerprint=str(row["portfolio_fingerprint"]),
            observed_at=_aware_utc(
                datetime.fromisoformat(str(row["observed_at"])), "observed_at"
            ),
        )

    def append(
        self,
        *,
        result: PortfolioReconciliationResult,
        broker_cash: Decimal,
        portfolio_fingerprint: str,
        observed_at: datetime,
    ) -> AccountReconciliationTruth:
        moment = _aware_utc(observed_at, "observed_at")
        if not broker_cash.is_finite() or broker_cash < 0:
            raise ValueError("broker_cash must be finite and non-negative")
        if not portfolio_fingerprint.strip():
            raise ValueError("portfolio_fingerprint is required")
        event_id = _event_id(
            result=result,
            broker_cash=broker_cash,
            portfolio_fingerprint=portfolio_fingerprint,
            observed_at=moment,
        )
        with self._transaction() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO paper_account_reconciliation_events
                (event_id, matched, cash_delta, position_mismatches, reasons,
                 broker_cash, portfolio_fingerprint, observed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    int(result.matched),
                    str(result.cash_delta),
                    len(result.position_deltas),
                    json.dumps(list(result.reasons), sort_keys=True),
                    str(broker_cash),
                    portfolio_fingerprint,
                    moment.isoformat(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM paper_account_reconciliation_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("ACCOUNT_RECONCILIATION_PERSISTENCE_FAILED")
            return self._row(row)

    def current(self) -> AccountReconciliationTruth | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT * FROM paper_account_reconciliation_events
                ORDER BY sequence DESC LIMIT 1"""
            ).fetchone()
            return None if row is None else self._row(row)
        finally:
            connection.close()


class PostgresAccountReconciliationTruthStore:
    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("dsn is required")
        if psycopg is None:
            raise RuntimeError(
                "install the postgresql extra to use PostgresAccountReconciliationTruthStore"
            )
        self.dsn = dsn

    def _connect(self):
        if psycopg is None or dict_row is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")
        return psycopg.connect(self.dsn, row_factory=dict_row, autocommit=False)

    def migrate(
        self,
        path: str | Path = "migrations/product/007_account_reconciliation_truth.sql",
    ) -> None:
        sql = Path(path).read_text(encoding="utf-8")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql)
            connection.commit()

    @staticmethod
    def _row(row: Mapping[str, object]) -> AccountReconciliationTruth:
        observed_at = row["observed_at"]
        if not isinstance(observed_at, datetime):
            observed_at = datetime.fromisoformat(str(observed_at))
        reasons = row["reasons"]
        if isinstance(reasons, str):
            reasons = json.loads(reasons)
        return AccountReconciliationTruth(
            sequence=int(row["sequence"]),
            event_id=str(row["event_id"]),
            matched=bool(row["matched"]),
            cash_delta=Decimal(str(row["cash_delta"])),
            position_mismatches=int(row["position_mismatches"]),
            reasons=tuple(str(reason) for reason in reasons),
            broker_cash=Decimal(str(row["broker_cash"])),
            portfolio_fingerprint=str(row["portfolio_fingerprint"]),
            observed_at=_aware_utc(observed_at, "observed_at"),
        )

    def append(
        self,
        *,
        result: PortfolioReconciliationResult,
        broker_cash: Decimal,
        portfolio_fingerprint: str,
        observed_at: datetime,
    ) -> AccountReconciliationTruth:
        moment = _aware_utc(observed_at, "observed_at")
        if not broker_cash.is_finite() or broker_cash < 0:
            raise ValueError("broker_cash must be finite and non-negative")
        if not portfolio_fingerprint.strip():
            raise ValueError("portfolio_fingerprint is required")
        event_id = _event_id(
            result=result,
            broker_cash=broker_cash,
            portfolio_fingerprint=portfolio_fingerprint,
            observed_at=moment,
        )
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """INSERT INTO paper_account_reconciliation_events
                    (event_id, matched, cash_delta, position_mismatches, reasons,
                     broker_cash, portfolio_fingerprint, observed_at)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                    ON CONFLICT (event_id) DO NOTHING""",
                    (
                        event_id,
                        result.matched,
                        result.cash_delta,
                        len(result.position_deltas),
                        json.dumps(list(result.reasons), sort_keys=True),
                        broker_cash,
                        portfolio_fingerprint,
                        moment,
                    ),
                )
                cursor.execute(
                    "SELECT * FROM paper_account_reconciliation_events WHERE event_id=%s",
                    (event_id,),
                )
                row = cursor.fetchone()
            connection.commit()
        if row is None:
            raise RuntimeError("ACCOUNT_RECONCILIATION_PERSISTENCE_FAILED")
        return self._row(row)

    def current(self) -> AccountReconciliationTruth | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT * FROM paper_account_reconciliation_events
                    ORDER BY sequence DESC LIMIT 1"""
                )
                row = cursor.fetchone()
        return None if row is None else self._row(row)


class AccountReconciliationGate:
    """Durable account-truth gate for risk-increasing paper actions."""

    def __init__(
        self,
        *,
        store: AccountReconciliationTruthStore,
        ledger: PortfolioLedger,
        maximum_age_seconds: Decimal,
    ) -> None:
        if not maximum_age_seconds.is_finite() or maximum_age_seconds < 0:
            raise ValueError("maximum_age_seconds must be finite and non-negative")
        self.store = store
        self.ledger = ledger
        self.maximum_age_seconds = maximum_age_seconds

    def reconcile(
        self,
        broker_truth: BrokerPortfolioTruth,
        *,
        occurred_at: datetime,
    ) -> PortfolioReconciliationResult:
        moment = _aware_utc(occurred_at, "occurred_at")
        result = reconcile_portfolio(self.ledger, broker_truth)
        self.store.append(
            result=result,
            broker_cash=broker_truth.cash,
            portfolio_fingerprint=portfolio_financial_fingerprint(self.ledger),
            observed_at=moment,
        )
        return result

    def authorize_intent(self, intent: OrderIntent, occurred_at: datetime) -> None:
        if intent.side is Side.BUY:
            self._assert_converged(occurred_at=occurred_at)

    def authorize_order(self, record: OrderRecord, occurred_at: datetime) -> None:
        if record.side is Side.BUY:
            self._assert_converged(occurred_at=occurred_at)

    def _assert_converged(self, *, occurred_at: datetime) -> AccountReconciliationTruth:
        moment = _aware_utc(occurred_at, "occurred_at")
        truth = self.store.current()
        if truth is None:
            raise AccountReconciliationBlocked(("ACCOUNT_RECONCILIATION_REQUIRED",))

        reasons: set[str] = set()
        if moment < truth.observed_at:
            reasons.add("ACCOUNT_RECONCILIATION_CLOCK_INVALID")
        else:
            age_seconds = Decimal(str((moment - truth.observed_at).total_seconds()))
            if age_seconds > self.maximum_age_seconds:
                reasons.add("ACCOUNT_RECONCILIATION_STALE")
        if truth.portfolio_fingerprint != portfolio_financial_fingerprint(self.ledger):
            reasons.add("ACCOUNT_RECONCILIATION_PORTFOLIO_CHANGED")
        if not truth.matched:
            reasons.add("ACCOUNT_RECONCILIATION_NOT_MATCHED")
            reasons.update(truth.reasons)
        if reasons:
            raise AccountReconciliationBlocked(tuple(sorted(reasons)))
        return truth
