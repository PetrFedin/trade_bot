from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from app.oms.reconciliation import (
    BrokerPortfolioTruth,
    PortfolioReconciliationResult,
    reconcile_portfolio,
)
from app.portfolio.ledger import PortfolioLedger

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None


@dataclass(frozen=True)
class PortfolioReconciliationEvidence:
    """Immutable proof of one broker-vs-durable-portfolio comparison."""

    reconciliation_id: str
    internal_cash: Decimal
    broker_cash: Decimal
    internal_positions: tuple[tuple[str, Decimal], ...]
    broker_positions: tuple[tuple[str, Decimal], ...]
    cash_delta: Decimal
    position_deltas: tuple[tuple[str, Decimal], ...]
    reasons: tuple[str, ...]
    cash_tolerance: Decimal
    quantity_tolerance: Decimal
    occurred_at: datetime

    @property
    def matched(self) -> bool:
        return not self.reasons

    def validate(self) -> None:
        if not self.reconciliation_id.strip():
            raise ValueError("reconciliation_id is required")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        for name, value in (
            ("internal_cash", self.internal_cash),
            ("broker_cash", self.broker_cash),
            ("cash_tolerance", self.cash_tolerance),
            ("quantity_tolerance", self.quantity_tolerance),
        ):
            if not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if not self.cash_delta.is_finite():
            raise ValueError("cash_delta must be finite")
        if self.cash_delta != self.broker_cash - self.internal_cash:
            raise ValueError("cash_delta disagrees with broker/internal cash")

        internal = _position_map(self.internal_positions, "internal_positions")
        broker = _position_map(self.broker_positions, "broker_positions")
        expected_deltas: list[tuple[str, Decimal]] = []
        expected_reasons: list[str] = []
        for symbol in sorted(set(internal) | set(broker)):
            delta = broker.get(symbol, Decimal("0")) - internal.get(symbol, Decimal("0"))
            if abs(delta) > self.quantity_tolerance:
                expected_deltas.append((symbol, delta))
                expected_reasons.append(f"POSITION_MISMATCH:{symbol}")
        if abs(self.cash_delta) > self.cash_tolerance:
            expected_reasons.append("CASH_MISMATCH")

        if self.position_deltas != tuple(expected_deltas):
            raise ValueError("position_deltas disagree with broker/internal positions")
        if self.reasons != tuple(expected_reasons):
            raise ValueError("reasons disagree with reconciliation economics")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "internal_cash": str(self.internal_cash),
            "broker_cash": str(self.broker_cash),
            "internal_positions": [[symbol, str(quantity)] for symbol, quantity in self.internal_positions],
            "broker_positions": [[symbol, str(quantity)] for symbol, quantity in self.broker_positions],
            "cash_delta": str(self.cash_delta),
            "position_deltas": [[symbol, str(delta)] for symbol, delta in self.position_deltas],
            "reasons": list(self.reasons),
            "cash_tolerance": str(self.cash_tolerance),
            "quantity_tolerance": str(self.quantity_tolerance),
        }

    @classmethod
    def from_payload(
        cls,
        *,
        reconciliation_id: str,
        payload: dict[str, object],
        occurred_at: datetime,
    ) -> "PortfolioReconciliationEvidence":
        def pairs(name: str) -> tuple[tuple[str, Decimal], ...]:
            raw = payload.get(name)
            if not isinstance(raw, list):
                raise ValueError(f"invalid {name}")
            result: list[tuple[str, Decimal]] = []
            for item in raw:
                if not isinstance(item, list) or len(item) != 2:
                    raise ValueError(f"invalid {name}")
                result.append((str(item[0]), Decimal(str(item[1]))))
            return tuple(result)

        raw_reasons = payload.get("reasons")
        if not isinstance(raw_reasons, list):
            raise ValueError("invalid reasons")
        evidence = cls(
            reconciliation_id=reconciliation_id,
            internal_cash=Decimal(str(payload["internal_cash"])),
            broker_cash=Decimal(str(payload["broker_cash"])),
            internal_positions=pairs("internal_positions"),
            broker_positions=pairs("broker_positions"),
            cash_delta=Decimal(str(payload["cash_delta"])),
            position_deltas=pairs("position_deltas"),
            reasons=tuple(str(reason) for reason in raw_reasons),
            cash_tolerance=Decimal(str(payload["cash_tolerance"])),
            quantity_tolerance=Decimal(str(payload["quantity_tolerance"])),
            occurred_at=occurred_at,
        )
        evidence.validate()
        return evidence


class PortfolioReconciliationStore(Protocol):
    def append(self, evidence: PortfolioReconciliationEvidence) -> bool: ...

    def latest(self) -> PortfolioReconciliationEvidence | None: ...


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("occurred_at must be timezone-aware")
    return value.astimezone(UTC)


def _position_map(
    positions: tuple[tuple[str, Decimal], ...],
    label: str,
) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    previous = ""
    for symbol, quantity in positions:
        if not symbol or symbol != symbol.upper():
            raise ValueError(f"{label} symbol must be non-empty uppercase")
        if symbol <= previous:
            raise ValueError(f"{label} must be strictly sorted without duplicates")
        if not quantity.is_finite() or quantity < 0:
            raise ValueError(f"{label} quantity must be finite and non-negative")
        result[symbol] = quantity
        previous = symbol
    return result


def build_portfolio_reconciliation_evidence(
    ledger: PortfolioLedger,
    broker: BrokerPortfolioTruth,
    result: PortfolioReconciliationResult,
    *,
    occurred_at: datetime,
    cash_tolerance: Decimal = Decimal("0.01"),
    quantity_tolerance: Decimal = Decimal("0"),
) -> PortfolioReconciliationEvidence:
    moment = _aware(occurred_at)
    expected = reconcile_portfolio(
        ledger,
        broker,
        cash_tolerance=cash_tolerance,
        quantity_tolerance=quantity_tolerance,
    )
    if result != expected:
        raise ValueError("reconciliation result disagrees with broker/internal truth")
    internal_positions = tuple(
        sorted((position.symbol, position.quantity) for position in ledger.positions())
    )
    broker_positions = tuple(
        sorted((position.symbol, position.quantity) for position in broker.positions)
    )
    evidence = PortfolioReconciliationEvidence(
        reconciliation_id=f"portfolio-reconciliation:{moment.isoformat()}",
        internal_cash=ledger.cash,
        broker_cash=broker.cash,
        internal_positions=internal_positions,
        broker_positions=broker_positions,
        cash_delta=result.cash_delta,
        position_deltas=result.position_deltas,
        reasons=result.reasons,
        cash_tolerance=cash_tolerance,
        quantity_tolerance=quantity_tolerance,
        occurred_at=moment,
    )
    evidence.validate()
    return evidence


class SQLitePortfolioReconciliationStore:
    """Append-only local evidence for broker portfolio reconciliation."""

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

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS portfolio_reconciliations (
                    reconciliation_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_portfolio_reconciliations_time
                ON portfolio_reconciliations(occurred_at, reconciliation_id);
                CREATE TRIGGER IF NOT EXISTS portfolio_reconciliations_no_update
                BEFORE UPDATE ON portfolio_reconciliations
                BEGIN
                    SELECT RAISE(ABORT, 'portfolio_reconciliations is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS portfolio_reconciliations_no_delete
                BEFORE DELETE ON portfolio_reconciliations
                BEGIN
                    SELECT RAISE(ABORT, 'portfolio_reconciliations is append-only');
                END;
                """
            )
        finally:
            connection.close()

    @staticmethod
    def _canonical(evidence: PortfolioReconciliationEvidence) -> str:
        return json.dumps(evidence.payload(), sort_keys=True, separators=(",", ":"))

    def append(self, evidence: PortfolioReconciliationEvidence) -> bool:
        evidence.validate()
        moment = _aware(evidence.occurred_at)
        payload = self._canonical(evidence)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """INSERT OR IGNORE INTO portfolio_reconciliations
                (reconciliation_id, payload, occurred_at) VALUES (?, ?, ?)""",
                (evidence.reconciliation_id, payload, moment.isoformat()),
            )
            appended = cursor.rowcount == 1
            if not appended:
                row = connection.execute(
                    """SELECT payload, occurred_at FROM portfolio_reconciliations
                    WHERE reconciliation_id=?""",
                    (evidence.reconciliation_id,),
                ).fetchone()
                if row is None:
                    raise RuntimeError("portfolio reconciliation lookup lost conflict")
                if str(row["payload"]) != payload or str(row["occurred_at"]) != moment.isoformat():
                    raise ValueError("PORTFOLIO_RECONCILIATION_CONFLICT")
            connection.execute("COMMIT")
            return appended
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        finally:
            connection.close()

    @staticmethod
    def _row(row: sqlite3.Row) -> PortfolioReconciliationEvidence:
        raw = json.loads(str(row["payload"]))
        if not isinstance(raw, dict):
            raise RuntimeError("invalid portfolio reconciliation payload")
        return PortfolioReconciliationEvidence.from_payload(
            reconciliation_id=str(row["reconciliation_id"]),
            payload=raw,
            occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
        )

    def latest(self) -> PortfolioReconciliationEvidence | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT reconciliation_id, payload, occurred_at
                FROM portfolio_reconciliations
                ORDER BY occurred_at DESC, reconciliation_id DESC LIMIT 1"""
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else self._row(row)


class PostgresPortfolioReconciliationStore:
    """PostgreSQL append-only reconciliation evidence with restart-safe latest truth."""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("dsn is required")
        if psycopg is None:
            raise RuntimeError("install the postgresql extra to use PostgresPortfolioReconciliationStore")
        self.dsn = dsn

    def _connect(self):
        if psycopg is None or dict_row is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")
        return psycopg.connect(self.dsn, row_factory=dict_row, autocommit=False)

    def migrate(self, path: str | Path = "migrations/product/007_portfolio_reconciliation.sql") -> None:
        sql = Path(path).read_text(encoding="utf-8")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql)
            connection.commit()

    def append(self, evidence: PortfolioReconciliationEvidence) -> bool:
        evidence.validate()
        moment = _aware(evidence.occurred_at)
        payload = evidence.payload()
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_portfolio_reconciliations
                        (reconciliation_id, payload, occurred_at)
                        VALUES (%s, %s::jsonb, %s)
                        ON CONFLICT (reconciliation_id) DO NOTHING""",
                        (
                            evidence.reconciliation_id,
                            json.dumps(payload, sort_keys=True),
                            moment,
                        ),
                    )
                    appended = cursor.rowcount == 1
                    if not appended:
                        cursor.execute(
                            """SELECT payload, occurred_at FROM astra_portfolio_reconciliations
                            WHERE reconciliation_id=%s FOR SHARE""",
                            (evidence.reconciliation_id,),
                        )
                        row = cursor.fetchone()
                        if row is None:
                            raise RuntimeError("portfolio reconciliation lookup lost conflict")
                        existing_payload = row["payload"]
                        if isinstance(existing_payload, str):
                            existing_payload = json.loads(existing_payload)
                        existing_time = row["occurred_at"]
                        if not isinstance(existing_time, datetime):
                            existing_time = datetime.fromisoformat(str(existing_time))
                        if existing_payload != payload or _aware(existing_time) != moment:
                            raise ValueError("PORTFOLIO_RECONCILIATION_CONFLICT")
                    return appended

    @staticmethod
    def _row(row: dict[str, object]) -> PortfolioReconciliationEvidence:
        raw = row["payload"]
        if isinstance(raw, str):
            raw = json.loads(raw)
        if not isinstance(raw, dict):
            raise RuntimeError("invalid portfolio reconciliation payload")
        occurred_at = row["occurred_at"]
        if not isinstance(occurred_at, datetime):
            occurred_at = datetime.fromisoformat(str(occurred_at))
        return PortfolioReconciliationEvidence.from_payload(
            reconciliation_id=str(row["reconciliation_id"]),
            payload=raw,
            occurred_at=occurred_at,
        )

    def latest(self) -> PortfolioReconciliationEvidence | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT reconciliation_id, payload, occurred_at
                    FROM astra_portfolio_reconciliations
                    ORDER BY occurred_at DESC, reconciliation_id DESC LIMIT 1"""
                )
                row = cursor.fetchone()
        return None if row is None else self._row(dict(row))
