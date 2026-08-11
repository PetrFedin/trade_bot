from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from app.domain.trading import TargetPosition
from app.portfolio.ledger import PortfolioLedger
from app.strategy.position_management import (
    ExitReason,
    PositionManagementPolicy,
    PositionTrackingState,
    evaluate_position_exit,
)


class PaperProtectionStatus(StrEnum):
    FLAT = "FLAT"
    TRACKING = "TRACKING"
    EXIT_PENDING = "EXIT_PENDING"


@dataclass(frozen=True)
class PaperProtectionState:
    strategy_id: str
    symbol: str
    tracked_quantity: Decimal
    average_cost: Decimal
    peak_reference_price: Decimal
    completed_bars_held: int
    last_observed_at: datetime
    last_completed_bar_at: datetime | None = None
    pending_exit_reason: ExitReason | None = None
    pending_target_price: Decimal | None = None
    pending_target_created_at: datetime | None = None
    pending_trigger_quantity: Decimal | None = None

    def validate(self) -> None:
        if not self.strategy_id.strip():
            raise ValueError("strategy_id is required")
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("symbol must be normalized uppercase")
        for name, value in (
            ("tracked_quantity", self.tracked_quantity),
            ("average_cost", self.average_cost),
            ("peak_reference_price", self.peak_reference_price),
        ):
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if self.completed_bars_held < 0:
            raise ValueError("completed_bars_held must be non-negative")
        _require_aware(self.last_observed_at, field_name="last_observed_at")
        if self.last_completed_bar_at is not None:
            _require_aware(
                self.last_completed_bar_at,
                field_name="last_completed_bar_at",
            )
            if self.last_completed_bar_at > self.last_observed_at:
                raise ValueError("completed bar cannot be later than last observation")
        pending_values = (
            self.pending_exit_reason,
            self.pending_target_price,
            self.pending_target_created_at,
            self.pending_trigger_quantity,
        )
        pending_count = sum(value is not None for value in pending_values)
        if pending_count not in (0, len(pending_values)):
            raise ValueError("pending protection fields must be all present or all absent")
        if self.pending_target_price is not None:
            if (
                not self.pending_target_price.is_finite()
                or self.pending_target_price <= 0
            ):
                raise ValueError("pending_target_price must be positive and finite")
        if self.pending_target_created_at is not None:
            _require_aware(
                self.pending_target_created_at,
                field_name="pending_target_created_at",
            )
        if self.pending_trigger_quantity is not None and (
            not self.pending_trigger_quantity.is_finite()
            or self.pending_trigger_quantity <= 0
        ):
            raise ValueError("pending_trigger_quantity must be positive and finite")


@dataclass(frozen=True)
class PaperProtectionDecision:
    status: PaperProtectionStatus
    state: PaperProtectionState | None
    exit_reason: ExitReason | None
    exit_target: TargetPosition | None
    trigger_quantity: Decimal | None
    protected_stop_price: Decimal | None
    profit_fraction: Decimal | None
    maximum_favorable_excursion_fraction: Decimal | None


class SQLitePaperProtectionStore:
    """Durable latest-state store for paper protection peaks and pending exits."""

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
            connection.execute(
                """CREATE TABLE IF NOT EXISTS paper_protection_state (
                    strategy_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    tracked_quantity TEXT NOT NULL,
                    average_cost TEXT NOT NULL,
                    peak_reference_price TEXT NOT NULL,
                    completed_bars_held INTEGER NOT NULL,
                    last_observed_at TEXT NOT NULL,
                    last_completed_bar_at TEXT,
                    pending_exit_reason TEXT,
                    pending_target_price TEXT,
                    pending_target_created_at TEXT,
                    pending_trigger_quantity TEXT,
                    PRIMARY KEY (strategy_id, symbol)
                )"""
            )
        finally:
            connection.close()

    def get(self, *, strategy_id: str, symbol: str) -> PaperProtectionState | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT * FROM paper_protection_state
                WHERE strategy_id=? AND symbol=?""",
                (strategy_id, symbol),
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else self._row(row)

    def upsert(self, state: PaperProtectionState) -> None:
        state.validate()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO paper_protection_state (
                    strategy_id, symbol, tracked_quantity, average_cost,
                    peak_reference_price, completed_bars_held, last_observed_at,
                    last_completed_bar_at, pending_exit_reason, pending_target_price,
                    pending_target_created_at, pending_trigger_quantity
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(strategy_id, symbol) DO UPDATE SET
                    tracked_quantity=excluded.tracked_quantity,
                    average_cost=excluded.average_cost,
                    peak_reference_price=excluded.peak_reference_price,
                    completed_bars_held=excluded.completed_bars_held,
                    last_observed_at=excluded.last_observed_at,
                    last_completed_bar_at=excluded.last_completed_bar_at,
                    pending_exit_reason=excluded.pending_exit_reason,
                    pending_target_price=excluded.pending_target_price,
                    pending_target_created_at=excluded.pending_target_created_at,
                    pending_trigger_quantity=excluded.pending_trigger_quantity""",
                (
                    state.strategy_id,
                    state.symbol,
                    str(state.tracked_quantity),
                    str(state.average_cost),
                    str(state.peak_reference_price),
                    state.completed_bars_held,
                    state.last_observed_at.astimezone(UTC).isoformat(),
                    _iso(state.last_completed_bar_at),
                    (
                        None
                        if state.pending_exit_reason is None
                        else state.pending_exit_reason.value
                    ),
                    (
                        None
                        if state.pending_target_price is None
                        else str(state.pending_target_price)
                    ),
                    _iso(state.pending_target_created_at),
                    (
                        None
                        if state.pending_trigger_quantity is None
                        else str(state.pending_trigger_quantity)
                    ),
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def delete(self, *, strategy_id: str, symbol: str) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "DELETE FROM paper_protection_state WHERE strategy_id=? AND symbol=?",
                (strategy_id, symbol),
            )
        finally:
            connection.close()

    @staticmethod
    def _row(row: sqlite3.Row) -> PaperProtectionState:
        reason = row["pending_exit_reason"]
        state = PaperProtectionState(
            strategy_id=str(row["strategy_id"]),
            symbol=str(row["symbol"]),
            tracked_quantity=Decimal(str(row["tracked_quantity"])),
            average_cost=Decimal(str(row["average_cost"])),
            peak_reference_price=Decimal(str(row["peak_reference_price"])),
            completed_bars_held=int(row["completed_bars_held"]),
            last_observed_at=datetime.fromisoformat(str(row["last_observed_at"])),
            last_completed_bar_at=_parse_optional_datetime(row["last_completed_bar_at"]),
            pending_exit_reason=None if reason is None else ExitReason(str(reason)),
            pending_target_price=_parse_optional_decimal(row["pending_target_price"]),
            pending_target_created_at=_parse_optional_datetime(
                row["pending_target_created_at"]
            ),
            pending_trigger_quantity=_parse_optional_decimal(
                row["pending_trigger_quantity"]
            ),
        )
        state.validate()
        return state


class PaperProtectionService:
    """Evaluate profit protection only from prices observable at decision time.

    The emitted exit target uses the *current observed reference price*, never the
    historical protected-stop level. This prevents paper execution from pretending a
    gap or already-passed stop was filled at an unavailable price. Pending exit state
    is durable and returns the original target on retry until the ledger is flat.
    """

    def __init__(
        self,
        *,
        ledger: PortfolioLedger,
        store: SQLitePaperProtectionStore,
        policy: PositionManagementPolicy,
        strategy_id: str = "cross-sectional-quality-v2-paper-shadow",
    ) -> None:
        policy.validate()
        if not strategy_id.strip():
            raise ValueError("strategy_id is required")
        self.ledger = ledger
        self.store = store
        self.policy = policy
        self.strategy_id = strategy_id.strip()

    def observe(
        self,
        *,
        symbol: str,
        reference_price: Decimal,
        observed_at: datetime,
        completed_bar_at: datetime | None = None,
    ) -> PaperProtectionDecision:
        normalized = symbol.strip().upper()
        if not normalized or normalized != symbol:
            raise ValueError("symbol must be normalized uppercase")
        if not reference_price.is_finite() or reference_price <= 0:
            raise ValueError("reference_price must be positive and finite")
        _require_aware(observed_at, field_name="observed_at")
        if completed_bar_at is not None:
            _require_aware(completed_bar_at, field_name="completed_bar_at")
            if completed_bar_at > observed_at:
                raise ValueError("completed bar cannot be later than observation")

        position = self.ledger.position(normalized)
        if position.quantity <= 0:
            self.store.delete(strategy_id=self.strategy_id, symbol=normalized)
            return PaperProtectionDecision(
                status=PaperProtectionStatus.FLAT,
                state=None,
                exit_reason=None,
                exit_target=None,
                trigger_quantity=None,
                protected_stop_price=None,
                profit_fraction=None,
                maximum_favorable_excursion_fraction=None,
            )

        prior = self.store.get(strategy_id=self.strategy_id, symbol=normalized)
        if prior is not None and observed_at < prior.last_observed_at:
            raise ValueError("stale paper protection observation")
        if prior is not None and prior.pending_exit_reason is not None:
            return self._pending_decision(prior)

        completed_bars = 0 if prior is None else prior.completed_bars_held
        last_completed = None if prior is None else prior.last_completed_bar_at
        if completed_bar_at is not None:
            if last_completed is not None and completed_bar_at < last_completed:
                raise ValueError("stale completed bar for paper protection")
            if last_completed is None or completed_bar_at > last_completed:
                completed_bars += 1
                last_completed = completed_bar_at

        prior_peak = (
            position.average_cost
            if prior is None
            else max(prior.peak_reference_price, position.average_cost)
        )
        decision = evaluate_position_exit(
            average_cost=position.average_cost,
            reference_price=reference_price,
            state=PositionTrackingState(
                entry_execution_index=0,
                peak_reference_price=prior_peak,
            ),
            current_execution_index=completed_bars,
            policy=self.policy,
        )
        pending_reason = decision.reason if decision.exit_now else None
        pending_price = reference_price if decision.exit_now else None
        pending_created_at = observed_at if decision.exit_now else None
        pending_quantity = position.quantity if decision.exit_now else None
        state = PaperProtectionState(
            strategy_id=self.strategy_id,
            symbol=normalized,
            tracked_quantity=position.quantity,
            average_cost=position.average_cost,
            peak_reference_price=decision.peak_reference_price,
            completed_bars_held=completed_bars,
            last_observed_at=observed_at,
            last_completed_bar_at=last_completed,
            pending_exit_reason=pending_reason,
            pending_target_price=pending_price,
            pending_target_created_at=pending_created_at,
            pending_trigger_quantity=pending_quantity,
        )
        self.store.upsert(state)
        if not decision.exit_now:
            return PaperProtectionDecision(
                status=PaperProtectionStatus.TRACKING,
                state=state,
                exit_reason=None,
                exit_target=None,
                trigger_quantity=None,
                protected_stop_price=decision.protected_stop_price,
                profit_fraction=decision.profit_fraction,
                maximum_favorable_excursion_fraction=(
                    decision.maximum_favorable_excursion_fraction
                ),
            )
        return PaperProtectionDecision(
            status=PaperProtectionStatus.EXIT_PENDING,
            state=state,
            exit_reason=decision.reason,
            exit_target=self._target_from_state(state),
            trigger_quantity=position.quantity,
            protected_stop_price=decision.protected_stop_price,
            profit_fraction=decision.profit_fraction,
            maximum_favorable_excursion_fraction=(
                decision.maximum_favorable_excursion_fraction
            ),
        )

    def _pending_decision(
        self,
        state: PaperProtectionState,
    ) -> PaperProtectionDecision:
        state.validate()
        return PaperProtectionDecision(
            status=PaperProtectionStatus.EXIT_PENDING,
            state=state,
            exit_reason=state.pending_exit_reason,
            exit_target=self._target_from_state(state),
            trigger_quantity=state.pending_trigger_quantity,
            protected_stop_price=None,
            profit_fraction=None,
            maximum_favorable_excursion_fraction=None,
        )

    def _target_from_state(self, state: PaperProtectionState) -> TargetPosition:
        if (
            state.pending_target_price is None
            or state.pending_target_created_at is None
        ):
            raise RuntimeError("pending protection state is missing target identity")
        return TargetPosition(
            symbol=state.symbol,
            quantity=Decimal("0"),
            reference_price=state.pending_target_price,
            generated_at=state.pending_target_created_at,
            strategy_id=f"{self.strategy_id}:protection",
        )


def _require_aware(value: datetime, *, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    _require_aware(value, field_name="timestamp")
    return value.astimezone(UTC).isoformat()


def _parse_optional_datetime(value: object) -> datetime | None:
    return None if value is None else datetime.fromisoformat(str(value))


def _parse_optional_decimal(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))
