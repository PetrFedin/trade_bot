from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.application.cross_sectional_paper_cycle import CrossSectionalPaperCycleResult
from app.domain.trading import Side


@dataclass(frozen=True)
class PaperDecisionAuditRecord:
    decision_id: str
    strategy_id: str
    generated_at: datetime
    selected_symbols: tuple[str, ...]
    targets: tuple[dict[str, str], ...]
    entry_blocks: tuple[tuple[str, str], ...]
    exit_reasons: tuple[tuple[str, str], ...]
    order_decisions: tuple[dict[str, Any], ...]
    prepared_intent_ids: tuple[str, ...]


class SQLitePaperDecisionAuditStore:
    """Idempotent JSON audit trail for selection, target and order decisions."""

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
                """CREATE TABLE IF NOT EXISTS paper_decision_audit (
                    decision_id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )"""
            )
        finally:
            connection.close()

    def append(self, record: PaperDecisionAuditRecord) -> bool:
        payload = _payload(record)
        rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload_json FROM paper_decision_audit WHERE decision_id=?",
                (record.decision_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_json"]) != rendered:
                    raise ValueError("PAPER_DECISION_AUDIT_CONFLICT")
                connection.execute("COMMIT")
                return False
            connection.execute(
                """INSERT INTO paper_decision_audit
                (decision_id, strategy_id, generated_at, payload_json)
                VALUES (?, ?, ?, ?)""",
                (
                    record.decision_id,
                    record.strategy_id,
                    record.generated_at.astimezone(UTC).isoformat(),
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

    def records(self, *, strategy_id: str) -> tuple[PaperDecisionAuditRecord, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """SELECT payload_json FROM paper_decision_audit
                WHERE strategy_id=? ORDER BY generated_at, decision_id""",
                (strategy_id,),
            ).fetchall()
        finally:
            connection.close()
        return tuple(_record(json.loads(str(row["payload_json"]))) for row in rows)


def audit_cross_sectional_paper_result(
    *,
    store: SQLitePaperDecisionAuditStore,
    strategy_id: str,
    generated_at: datetime,
    result: CrossSectionalPaperCycleResult,
) -> PaperDecisionAuditRecord:
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("generated_at must be timezone-aware")
    if not strategy_id.strip():
        raise ValueError("strategy_id is required")
    targets = tuple(
        {
            "symbol": target.symbol,
            "quantity": str(target.quantity),
            "reference_price": str(target.reference_price),
            "generated_at": target.generated_at.astimezone(UTC).isoformat(),
        }
        for target in result.target_plan.targets
    )
    entry_blocks = tuple(
        (symbol, reason.value)
        for symbol, reason in result.target_plan.entry_blocks
    )
    exit_reasons = tuple(
        (symbol, reason.value)
        for symbol, reason in result.target_plan.exit_reasons
    )
    order_decisions = tuple(
        {
            "symbol": item.target.symbol,
            "approved": item.approved,
            "reasons": list(item.reasons),
            "side": None if item.intent is None else item.intent.side.value,
            "intent_id": None if item.intent is None else item.intent.intent_id,
            "quantity": None if item.intent is None else str(item.intent.quantity),
            "limit_price": None if item.intent is None else str(item.intent.limit_price),
        }
        for item in result.order_plan.items
    )
    prepared_intent_ids = tuple(
        prepared.record.intent_id for prepared in result.prepared_orders
    )
    decision_id = _decision_id(
        strategy_id=strategy_id,
        generated_at=generated_at,
        selected_symbols=result.target_plan.selected_symbols,
        targets=targets,
    )
    record = PaperDecisionAuditRecord(
        decision_id=decision_id,
        strategy_id=strategy_id.strip(),
        generated_at=generated_at.astimezone(UTC),
        selected_symbols=result.target_plan.selected_symbols,
        targets=targets,
        entry_blocks=entry_blocks,
        exit_reasons=exit_reasons,
        order_decisions=order_decisions,
        prepared_intent_ids=prepared_intent_ids,
    )
    store.append(record)
    return record


def _decision_id(
    *,
    strategy_id: str,
    generated_at: datetime,
    selected_symbols: tuple[str, ...],
    targets: tuple[dict[str, str], ...],
) -> str:
    canonical = json.dumps(
        {
            "strategy_id": strategy_id,
            "generated_at": generated_at.astimezone(UTC).isoformat(),
            "selected_symbols": list(selected_symbols),
            "targets": targets,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return f"paper-decision:{digest}"


def _payload(record: PaperDecisionAuditRecord) -> dict[str, Any]:
    return {
        "decision_id": record.decision_id,
        "strategy_id": record.strategy_id,
        "generated_at": record.generated_at.astimezone(UTC).isoformat(),
        "selected_symbols": list(record.selected_symbols),
        "targets": list(record.targets),
        "entry_blocks": [list(item) for item in record.entry_blocks],
        "exit_reasons": [list(item) for item in record.exit_reasons],
        "order_decisions": list(record.order_decisions),
        "prepared_intent_ids": list(record.prepared_intent_ids),
    }


def _record(payload: dict[str, Any]) -> PaperDecisionAuditRecord:
    return PaperDecisionAuditRecord(
        decision_id=str(payload["decision_id"]),
        strategy_id=str(payload["strategy_id"]),
        generated_at=datetime.fromisoformat(str(payload["generated_at"])),
        selected_symbols=tuple(str(item) for item in payload["selected_symbols"]),
        targets=tuple(dict(item) for item in payload["targets"]),
        entry_blocks=tuple(
            (str(item[0]), str(item[1])) for item in payload["entry_blocks"]
        ),
        exit_reasons=tuple(
            (str(item[0]), str(item[1])) for item in payload["exit_reasons"]
        ),
        order_decisions=tuple(dict(item) for item in payload["order_decisions"]),
        prepared_intent_ids=tuple(
            str(item) for item in payload["prepared_intent_ids"]
        ),
    )
