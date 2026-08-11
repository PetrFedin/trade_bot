from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from app.strategy.cross_sectional_portfolio import PortfolioEntryBlockReason
from app.strategy.cross_sectional_selection import CrossSectionalSelection
from app.strategy.reentry_confirmation import ReentryConfirmationPolicy


class PaperReentryEventKind(StrEnum):
    EXIT_FILL = "EXIT_FILL"
    ENTRY_FILL = "ENTRY_FILL"


@dataclass(frozen=True)
class PaperReentryState:
    strategy_id: str
    symbol: str
    consecutive_selected_decisions: int
    armed_at: datetime
    last_exit_event_id: str
    last_decision_time: datetime | None = None

    def validate(self) -> None:
        if not self.strategy_id.strip():
            raise ValueError("strategy_id is required")
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("symbol must be normalized uppercase")
        if self.consecutive_selected_decisions < 0:
            raise ValueError("confirmation streak must be non-negative")
        _require_aware(self.armed_at, field_name="armed_at")
        if not self.last_exit_event_id.strip():
            raise ValueError("last_exit_event_id is required")
        if self.last_decision_time is not None:
            _require_aware(self.last_decision_time, field_name="last_decision_time")


@dataclass(frozen=True)
class PaperReentryEventResult:
    applied: bool
    state: PaperReentryState | None


@dataclass(frozen=True)
class PaperReentryDecision:
    symbol: str
    selected: bool
    allow_entry: bool
    reason: PortfolioEntryBlockReason | None
    confirmation_streak: int
    decision_time: datetime
    state: PaperReentryState


class SQLitePaperReentryStore:
    """Durable re-entry state plus idempotent entry/exit fill event journal."""

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
                CREATE TABLE IF NOT EXISTS paper_reentry_state (
                    strategy_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    consecutive_selected_decisions INTEGER NOT NULL,
                    armed_at TEXT NOT NULL,
                    last_exit_event_id TEXT NOT NULL,
                    last_decision_time TEXT,
                    PRIMARY KEY (strategy_id, symbol)
                );
                CREATE TABLE IF NOT EXISTS paper_reentry_events (
                    event_id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    event_kind TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                """
            )
        finally:
            connection.close()

    def state(self, *, strategy_id: str, symbol: str) -> PaperReentryState | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT * FROM paper_reentry_state
                WHERE strategy_id=? AND symbol=?""",
                (strategy_id, symbol),
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else self._state_row(row)

    def states(self, *, strategy_id: str) -> tuple[PaperReentryState, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """SELECT * FROM paper_reentry_state
                WHERE strategy_id=? ORDER BY symbol""",
                (strategy_id,),
            ).fetchall()
        finally:
            connection.close()
        return tuple(self._state_row(row) for row in rows)

    def apply_fill_event(
        self,
        *,
        event_id: str,
        strategy_id: str,
        symbol: str,
        event_kind: PaperReentryEventKind,
        occurred_at: datetime,
    ) -> PaperReentryEventResult:
        if not event_id.strip() or not strategy_id.strip():
            raise ValueError("re-entry event and strategy identity are required")
        if not symbol or symbol != symbol.strip().upper():
            raise ValueError("symbol must be normalized uppercase")
        _require_aware(occurred_at, field_name="occurred_at")
        moment = occurred_at.astimezone(UTC)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM paper_reentry_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if existing is not None:
                expected = (
                    strategy_id,
                    symbol,
                    event_kind.value,
                    moment.isoformat(),
                )
                actual = (
                    str(existing["strategy_id"]),
                    str(existing["symbol"]),
                    str(existing["event_kind"]),
                    str(existing["occurred_at"]),
                )
                if actual != expected:
                    raise ValueError("PAPER_REENTRY_EVENT_CONFLICT")
                state_row = connection.execute(
                    """SELECT * FROM paper_reentry_state
                    WHERE strategy_id=? AND symbol=?""",
                    (strategy_id, symbol),
                ).fetchone()
                connection.execute("COMMIT")
                return PaperReentryEventResult(
                    applied=False,
                    state=None if state_row is None else self._state_row(state_row),
                )

            connection.execute(
                """INSERT INTO paper_reentry_events
                (event_id, strategy_id, symbol, event_kind, occurred_at)
                VALUES (?, ?, ?, ?, ?)""",
                (
                    event_id,
                    strategy_id,
                    symbol,
                    event_kind.value,
                    moment.isoformat(),
                ),
            )
            if event_kind is PaperReentryEventKind.EXIT_FILL:
                connection.execute(
                    """INSERT INTO paper_reentry_state (
                        strategy_id, symbol, consecutive_selected_decisions,
                        armed_at, last_exit_event_id, last_decision_time
                    ) VALUES (?, ?, 0, ?, ?, NULL)
                    ON CONFLICT(strategy_id, symbol) DO UPDATE SET
                        consecutive_selected_decisions=0,
                        armed_at=excluded.armed_at,
                        last_exit_event_id=excluded.last_exit_event_id,
                        last_decision_time=NULL""",
                    (strategy_id, symbol, moment.isoformat(), event_id),
                )
            else:
                connection.execute(
                    "DELETE FROM paper_reentry_state WHERE strategy_id=? AND symbol=?",
                    (strategy_id, symbol),
                )
            state_row = connection.execute(
                """SELECT * FROM paper_reentry_state
                WHERE strategy_id=? AND symbol=?""",
                (strategy_id, symbol),
            ).fetchone()
            connection.execute("COMMIT")
            return PaperReentryEventResult(
                applied=True,
                state=None if state_row is None else self._state_row(state_row),
            )
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def advance_decision(
        self,
        *,
        strategy_id: str,
        symbol: str,
        selected: bool,
        decision_time: datetime,
    ) -> PaperReentryState | None:
        _require_aware(decision_time, field_name="decision_time")
        moment = decision_time.astimezone(UTC)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT * FROM paper_reentry_state
                WHERE strategy_id=? AND symbol=?""",
                (strategy_id, symbol),
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return None
            state = self._state_row(row)
            if state.last_decision_time is not None:
                previous = state.last_decision_time.astimezone(UTC)
                if moment < previous:
                    raise ValueError("stale paper re-entry decision")
                if moment == previous:
                    connection.execute("COMMIT")
                    return state
            streak = state.consecutive_selected_decisions + 1 if selected else 0
            connection.execute(
                """UPDATE paper_reentry_state
                SET consecutive_selected_decisions=?, last_decision_time=?
                WHERE strategy_id=? AND symbol=?""",
                (streak, moment.isoformat(), strategy_id, symbol),
            )
            row = connection.execute(
                """SELECT * FROM paper_reentry_state
                WHERE strategy_id=? AND symbol=?""",
                (strategy_id, symbol),
            ).fetchone()
            connection.execute("COMMIT")
            if row is None:
                raise RuntimeError("paper re-entry state vanished during update")
            return self._state_row(row)
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @staticmethod
    def _state_row(row: sqlite3.Row) -> PaperReentryState:
        last_decision = row["last_decision_time"]
        state = PaperReentryState(
            strategy_id=str(row["strategy_id"]),
            symbol=str(row["symbol"]),
            consecutive_selected_decisions=int(
                row["consecutive_selected_decisions"]
            ),
            armed_at=datetime.fromisoformat(str(row["armed_at"])),
            last_exit_event_id=str(row["last_exit_event_id"]),
            last_decision_time=(
                None
                if last_decision is None
                else datetime.fromisoformat(str(last_decision))
            ),
        )
        state.validate()
        return state


class PaperReentryController:
    """Paper-safe re-entry confirmation that clears only on an actual entry fill."""

    def __init__(
        self,
        *,
        store: SQLitePaperReentryStore,
        policy: ReentryConfirmationPolicy,
        strategy_id: str = "cross-sectional-quality-v2-paper-shadow",
    ) -> None:
        policy.validate()
        if not strategy_id.strip():
            raise ValueError("strategy_id is required")
        self.store = store
        self.policy = policy
        self.strategy_id = strategy_id.strip()

    def record_exit_fill(
        self,
        *,
        event_id: str,
        symbol: str,
        occurred_at: datetime,
    ) -> PaperReentryEventResult:
        return self.store.apply_fill_event(
            event_id=event_id,
            strategy_id=self.strategy_id,
            symbol=symbol,
            event_kind=PaperReentryEventKind.EXIT_FILL,
            occurred_at=occurred_at,
        )

    def record_entry_fill(
        self,
        *,
        event_id: str,
        symbol: str,
        occurred_at: datetime,
    ) -> PaperReentryEventResult:
        return self.store.apply_fill_event(
            event_id=event_id,
            strategy_id=self.strategy_id,
            symbol=symbol,
            event_kind=PaperReentryEventKind.ENTRY_FILL,
            occurred_at=occurred_at,
        )

    def evaluate_selection(
        self,
        selection: CrossSectionalSelection,
    ) -> tuple[PaperReentryDecision, ...]:
        selected_symbols = set(selection.selected_symbols)
        decisions: list[PaperReentryDecision] = []
        for prior in self.store.states(strategy_id=self.strategy_id):
            selected = prior.symbol in selected_symbols
            state = self.store.advance_decision(
                strategy_id=self.strategy_id,
                symbol=prior.symbol,
                selected=selected,
                decision_time=selection.decision_time,
            )
            if state is None:
                continue
            allow_entry = (
                selected
                and state.consecutive_selected_decisions
                >= self.policy.minimum_consecutive_eligible_bars
            )
            reason = (
                PortfolioEntryBlockReason.REENTRY_CONFIRMATION_PENDING
                if selected and not allow_entry
                else None
            )
            decisions.append(
                PaperReentryDecision(
                    symbol=state.symbol,
                    selected=selected,
                    allow_entry=allow_entry,
                    reason=reason,
                    confirmation_streak=state.consecutive_selected_decisions,
                    decision_time=selection.decision_time,
                    state=state,
                )
            )
        return tuple(decisions)

    def blocks_for_selection(
        self,
        selection: CrossSectionalSelection,
    ) -> dict[str, PortfolioEntryBlockReason]:
        return {
            decision.symbol: decision.reason
            for decision in self.evaluate_selection(selection)
            if decision.reason is not None
        }


def _require_aware(value: datetime, *, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
