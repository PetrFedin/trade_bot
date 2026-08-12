from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from app.application.cross_sectional_target_planner import CrossSectionalTargetPlan
from app.marketdata.ohlcv import OhlcvBar
from app.portfolio.ledger import PortfolioLedger
from app.strategy.entry_quality import EntryQualityFilteredSelector
from app.strategy.selection_exit_confirmation import (
    SelectionExitConfirmationPolicy,
    SelectionExitConfirmationState,
    evaluate_selection_exit_confirmation,
)


class PaperCandidateKind(StrEnum):
    ENTRY_QUALITY = "ENTRY_QUALITY"
    SELECTION_EXIT_CONFIRMATION = "SELECTION_EXIT_CONFIRMATION"


@dataclass(frozen=True)
class PaperCandidateShadowRecord:
    record_id: str
    strategy_id: str
    candidate: PaperCandidateKind
    decision_time: datetime
    observed_at: datetime
    symbol: str
    baseline_action: str
    candidate_action: str
    reasons: tuple[str, ...]
    metrics: dict[str, str | int | bool | None]
    evidence_scope: str = "DECISION_DIVERGENCE_ONLY"


class SQLitePaperCandidateShadowStore:
    """Durable idempotent evidence for candidates that never mutate paper orders."""

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
                """CREATE TABLE IF NOT EXISTS paper_candidate_shadow (
                    record_id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    candidate TEXT NOT NULL,
                    decision_time TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )"""
            )
        finally:
            connection.close()

    def append(self, record: PaperCandidateShadowRecord) -> bool:
        _validate_record(record)
        rendered = json.dumps(
            _record_payload(record), sort_keys=True, separators=(",", ":")
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload_json FROM paper_candidate_shadow WHERE record_id=?",
                (record.record_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_json"]) != rendered:
                    raise ValueError("PAPER_CANDIDATE_SHADOW_RECORD_CONFLICT")
                connection.execute("COMMIT")
                return False
            connection.execute(
                """INSERT INTO paper_candidate_shadow (
                    record_id, strategy_id, candidate, decision_time,
                    observed_at, symbol, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.record_id,
                    record.strategy_id,
                    record.candidate.value,
                    record.decision_time.astimezone(UTC).isoformat(),
                    record.observed_at.astimezone(UTC).isoformat(),
                    record.symbol,
                    rendered,
                ),
            )
            connection.execute("COMMIT")
            return True
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def records(
        self,
        *,
        strategy_id: str,
        candidate: PaperCandidateKind | None = None,
    ) -> tuple[PaperCandidateShadowRecord, ...]:
        connection = self._connect()
        try:
            if candidate is None:
                rows = connection.execute(
                    """SELECT payload_json FROM paper_candidate_shadow
                    WHERE strategy_id=?
                    ORDER BY decision_time, candidate, symbol, record_id""",
                    (strategy_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT payload_json FROM paper_candidate_shadow
                    WHERE strategy_id=? AND candidate=?
                    ORDER BY decision_time, symbol, record_id""",
                    (strategy_id, candidate.value),
                ).fetchall()
        finally:
            connection.close()
        return tuple(_record_from_payload(json.loads(row["payload_json"])) for row in rows)


@dataclass(frozen=True)
class SelectionExitShadowState:
    strategy_id: str
    symbol: str
    state: SelectionExitConfirmationState
    updated_at: datetime


class SQLiteSelectionExitShadowStateStore:
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
                """CREATE TABLE IF NOT EXISTS selection_exit_shadow_state (
                    strategy_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    consecutive_deselected_bars INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (strategy_id, symbol)
                )"""
            )
        finally:
            connection.close()

    def get(self, *, strategy_id: str, symbol: str) -> SelectionExitConfirmationState:
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT consecutive_deselected_bars
                FROM selection_exit_shadow_state
                WHERE strategy_id=? AND symbol=?""",
                (strategy_id, symbol),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return SelectionExitConfirmationState()
        state = SelectionExitConfirmationState(
            consecutive_deselected_bars=int(row["consecutive_deselected_bars"])
        )
        state.validate()
        return state

    def put(
        self,
        *,
        strategy_id: str,
        symbol: str,
        state: SelectionExitConfirmationState,
        updated_at: datetime,
    ) -> None:
        state.validate()
        _validate_identity(strategy_id=strategy_id, symbol=symbol, timestamp=updated_at)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO selection_exit_shadow_state (
                    strategy_id, symbol, consecutive_deselected_bars, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(strategy_id, symbol) DO UPDATE SET
                    consecutive_deselected_bars=excluded.consecutive_deselected_bars,
                    updated_at=excluded.updated_at""",
                (
                    strategy_id,
                    symbol,
                    state.consecutive_deselected_bars,
                    updated_at.astimezone(UTC).isoformat(),
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def clear_inactive(
        self,
        *,
        strategy_id: str,
        active_symbols: set[str],
    ) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT symbol FROM selection_exit_shadow_state WHERE strategy_id=?",
                (strategy_id,),
            ).fetchall()
            for row in rows:
                symbol = str(row["symbol"])
                if symbol not in active_symbols:
                    connection.execute(
                        """DELETE FROM selection_exit_shadow_state
                        WHERE strategy_id=? AND symbol=?""",
                        (strategy_id, symbol),
                    )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()


class PaperCandidateShadowObserver(Protocol):
    name: str

    def observe(
        self,
        bars: tuple[OhlcvBar, ...],
        *,
        baseline_plan: CrossSectionalTargetPlan,
        observed_at: datetime,
    ) -> tuple[PaperCandidateShadowRecord, ...]: ...


@dataclass(frozen=True)
class PaperCandidateShadowFailure:
    observer: str
    error_type: str
    message: str


@dataclass(frozen=True)
class PaperCandidateShadowBatch:
    records: tuple[PaperCandidateShadowRecord, ...]
    failures: tuple[PaperCandidateShadowFailure, ...]

    @property
    def divergence_count(self) -> int:
        return sum(record.baseline_action != record.candidate_action for record in self.records)


class PaperCandidateShadowSuite:
    """Run counterfactual observers after baseline outbox without changing execution."""

    def __init__(self, observers: Iterable[PaperCandidateShadowObserver]) -> None:
        self.observers = tuple(observers)

    def observe(
        self,
        bars: tuple[OhlcvBar, ...],
        *,
        baseline_plan: CrossSectionalTargetPlan,
        observed_at: datetime,
    ) -> PaperCandidateShadowBatch:
        records: list[PaperCandidateShadowRecord] = []
        failures: list[PaperCandidateShadowFailure] = []
        for observer in self.observers:
            try:
                records.extend(
                    observer.observe(
                        bars,
                        baseline_plan=baseline_plan,
                        observed_at=observed_at,
                    )
                )
            except Exception as exc:
                failures.append(
                    PaperCandidateShadowFailure(
                        observer=observer.name,
                        error_type=type(exc).__name__,
                        message=str(exc),
                    )
                )
        return PaperCandidateShadowBatch(
            records=tuple(records),
            failures=tuple(failures),
        )


class EntryQualityPaperShadowObserver:
    name = PaperCandidateKind.ENTRY_QUALITY.value

    def __init__(
        self,
        *,
        strategy_id: str,
        selector: EntryQualityFilteredSelector,
        store: SQLitePaperCandidateShadowStore,
    ) -> None:
        if not strategy_id.strip():
            raise ValueError("strategy_id is required")
        self.strategy_id = strategy_id.strip()
        self.selector = selector
        self.store = store

    def observe(
        self,
        bars: tuple[OhlcvBar, ...],
        *,
        baseline_plan: CrossSectionalTargetPlan,
        observed_at: datetime,
    ) -> tuple[PaperCandidateShadowRecord, ...]:
        _, trace = self.selector.select_with_trace(bars)
        baseline_selected = set(baseline_plan.selected_symbols)
        records: list[PaperCandidateShadowRecord] = []
        for evaluation in trace.evaluations:
            baseline_action = (
                "SELECT" if evaluation.symbol in baseline_selected else "SKIP"
            )
            candidate_action = "SELECT" if evaluation.selected else "SKIP"
            metrics = {
                key: _scalar(value)
                for key, value in asdict(evaluation.metrics).items()
                if key != "symbol"
            }
            record = _candidate_record(
                strategy_id=self.strategy_id,
                candidate=PaperCandidateKind.ENTRY_QUALITY,
                decision_time=baseline_plan.decision_time,
                observed_at=observed_at,
                symbol=evaluation.symbol,
                baseline_action=baseline_action,
                candidate_action=candidate_action,
                reasons=tuple(reason.value for reason in evaluation.block_reasons),
                metrics=metrics,
            )
            self.store.append(record)
            records.append(record)
        return tuple(records)


class SelectionExitPaperShadowObserver:
    name = PaperCandidateKind.SELECTION_EXIT_CONFIRMATION.value

    def __init__(
        self,
        *,
        strategy_id: str,
        ledger: PortfolioLedger,
        policy: SelectionExitConfirmationPolicy,
        state_store: SQLiteSelectionExitShadowStateStore,
        evidence_store: SQLitePaperCandidateShadowStore,
    ) -> None:
        if not strategy_id.strip():
            raise ValueError("strategy_id is required")
        policy.validate()
        self.strategy_id = strategy_id.strip()
        self.ledger = ledger
        self.policy = policy
        self.state_store = state_store
        self.evidence_store = evidence_store

    def observe(
        self,
        bars: tuple[OhlcvBar, ...],
        *,
        baseline_plan: CrossSectionalTargetPlan,
        observed_at: datetime,
    ) -> tuple[PaperCandidateShadowRecord, ...]:
        decision_closes = _decision_closes(bars, baseline_plan.decision_time)
        selected = set(baseline_plan.selected_symbols)
        exit_reasons = dict(baseline_plan.exit_reasons)
        active = {
            position.symbol
            for position in self.ledger.positions()
            if position.quantity > 0 and position.symbol in decision_closes
        }
        self.state_store.clear_inactive(
            strategy_id=self.strategy_id,
            active_symbols=active,
        )
        records: list[PaperCandidateShadowRecord] = []
        for symbol in sorted(active):
            position = self.ledger.position(symbol)
            baseline_reason = exit_reasons.get(symbol)
            baseline_action = (
                "HOLD"
                if baseline_reason is None
                else f"EXIT:{baseline_reason.value}"
            )
            prior = self.state_store.get(
                strategy_id=self.strategy_id,
                symbol=symbol,
            )
            if baseline_reason is not None and baseline_reason.value != "SELECTION_EXIT":
                candidate_action = baseline_action
                candidate_reason = "RISK_OR_PROTECTIVE_EXIT_UNCHANGED"
                next_state = SelectionExitConfirmationState()
            else:
                decision = evaluate_selection_exit_confirmation(
                    selected=symbol in selected,
                    profitable_at_decision=(
                        decision_closes[symbol] > position.average_cost
                    ),
                    state=prior,
                    policy=self.policy,
                )
                next_state = decision.state
                candidate_reason = decision.reason
                if decision.allow_selection_exit:
                    candidate_action = "EXIT:SELECTION_EXIT"
                elif symbol not in selected:
                    candidate_action = "PENDING:SELECTION_EXIT"
                else:
                    candidate_action = "HOLD"
            self.state_store.put(
                strategy_id=self.strategy_id,
                symbol=symbol,
                state=next_state,
                updated_at=observed_at,
            )
            record = _candidate_record(
                strategy_id=self.strategy_id,
                candidate=PaperCandidateKind.SELECTION_EXIT_CONFIRMATION,
                decision_time=baseline_plan.decision_time,
                observed_at=observed_at,
                symbol=symbol,
                baseline_action=baseline_action,
                candidate_action=candidate_action,
                reasons=(candidate_reason,),
                metrics={
                    "decision_close": str(decision_closes[symbol]),
                    "average_cost": str(position.average_cost),
                    "profitable_at_decision": (
                        decision_closes[symbol] > position.average_cost
                    ),
                    "prior_deselection_streak": prior.consecutive_deselected_bars,
                    "next_deselection_streak": (
                        next_state.consecutive_deselected_bars
                    ),
                },
            )
            self.evidence_store.append(record)
            records.append(record)
        return tuple(records)


def _candidate_record(
    *,
    strategy_id: str,
    candidate: PaperCandidateKind,
    decision_time: datetime,
    observed_at: datetime,
    symbol: str,
    baseline_action: str,
    candidate_action: str,
    reasons: tuple[str, ...],
    metrics: dict[str, str | int | bool | None],
) -> PaperCandidateShadowRecord:
    canonical = "|".join(
        (
            strategy_id,
            candidate.value,
            decision_time.astimezone(UTC).isoformat(),
            symbol,
        )
    )
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return PaperCandidateShadowRecord(
        record_id=f"paper-candidate:{digest}",
        strategy_id=strategy_id,
        candidate=candidate,
        decision_time=decision_time,
        observed_at=observed_at,
        symbol=symbol,
        baseline_action=baseline_action,
        candidate_action=candidate_action,
        reasons=reasons,
        metrics=metrics,
    )


def _decision_closes(
    bars: tuple[OhlcvBar, ...],
    decision_time: datetime,
) -> dict[str, Decimal]:
    closes = {
        bar.symbol: bar.close
        for bar in bars
        if bar.timestamp == decision_time
    }
    if not closes:
        raise ValueError("candidate shadow requires completed decision-bar closes")
    return closes


def _scalar(value: object) -> str | int | bool | None:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"unsupported candidate metric type:{type(value).__name__}")


def _validate_identity(*, strategy_id: str, symbol: str, timestamp: datetime) -> None:
    if not strategy_id.strip():
        raise ValueError("strategy_id is required")
    if not symbol or symbol != symbol.strip().upper():
        raise ValueError("candidate shadow symbol must be normalized uppercase")
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("candidate shadow timestamp must be timezone-aware")


def _validate_record(record: PaperCandidateShadowRecord) -> None:
    if not record.record_id.strip():
        raise ValueError("candidate shadow record_id is required")
    _validate_identity(
        strategy_id=record.strategy_id,
        symbol=record.symbol,
        timestamp=record.decision_time,
    )
    if record.observed_at.tzinfo is None or record.observed_at.utcoffset() is None:
        raise ValueError("candidate shadow observed_at must be timezone-aware")
    if record.observed_at < record.decision_time:
        raise ValueError("candidate shadow observation cannot precede decision")
    if not record.baseline_action or not record.candidate_action:
        raise ValueError("candidate shadow actions are required")
    if record.evidence_scope != "DECISION_DIVERGENCE_ONLY":
        raise ValueError("candidate shadow evidence scope must remain decision-only")


def _record_payload(record: PaperCandidateShadowRecord) -> dict[str, Any]:
    return {
        "record_id": record.record_id,
        "strategy_id": record.strategy_id,
        "candidate": record.candidate.value,
        "decision_time": record.decision_time.astimezone(UTC).isoformat(),
        "observed_at": record.observed_at.astimezone(UTC).isoformat(),
        "symbol": record.symbol,
        "baseline_action": record.baseline_action,
        "candidate_action": record.candidate_action,
        "reasons": list(record.reasons),
        "metrics": record.metrics,
        "evidence_scope": record.evidence_scope,
    }


def _record_from_payload(payload: Mapping[str, Any]) -> PaperCandidateShadowRecord:
    record = PaperCandidateShadowRecord(
        record_id=str(payload["record_id"]),
        strategy_id=str(payload["strategy_id"]),
        candidate=PaperCandidateKind(str(payload["candidate"])),
        decision_time=datetime.fromisoformat(str(payload["decision_time"])),
        observed_at=datetime.fromisoformat(str(payload["observed_at"])),
        symbol=str(payload["symbol"]),
        baseline_action=str(payload["baseline_action"]),
        candidate_action=str(payload["candidate_action"]),
        reasons=tuple(str(item) for item in payload["reasons"]),
        metrics=dict(payload["metrics"]),
        evidence_scope=str(payload["evidence_scope"]),
    )
    _validate_record(record)
    return record
