from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Protocol

from app.domain.trading import OrderIntent
from app.risk.pretrade import PreTradeRiskEngine, RiskContext, RiskDecision

ZERO_DIGEST = "0" * 64


def _normalize(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("risk evidence timestamps must be timezone-aware")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return _normalize(asdict(value))
    if isinstance(value, Mapping):
        ordered = sorted(value.items(), key=lambda pair: str(pair[0]))
        return {str(key): _normalize(item) for key, item in ordered}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_normalize(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported risk evidence value: {type(value).__name__}")


def canonical_json(value: object) -> str:
    return json.dumps(
        _normalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RiskEvidenceRecord:
    sequence: int
    decision_id: str
    intent_id: str
    payload: Mapping[str, object]
    created_at: datetime
    previous_digest: str
    digest: str

    def unsigned(self) -> Mapping[str, object]:
        return {
            "sequence": self.sequence,
            "decision_id": self.decision_id,
            "intent_id": self.intent_id,
            "payload": dict(self.payload),
            "created_at": self.created_at,
            "previous_digest": self.previous_digest,
        }


@dataclass(frozen=True)
class RecordedRiskDecision:
    decision: RiskDecision
    decision_id: str
    evidence_digest: str


class RiskEvidenceJournal(Protocol):
    def append(
        self,
        *,
        decision_id: str,
        intent_id: str,
        payload: Mapping[str, object],
        created_at: datetime,
    ) -> RiskEvidenceRecord: ...

    def verify(self) -> tuple[RiskEvidenceRecord, ...]: ...


class SQLiteRiskEvidenceJournal:
    """Append-only hash-chained SQLite risk-decision journal."""

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
                """CREATE TABLE IF NOT EXISTS risk_decisions (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    decision_id TEXT NOT NULL UNIQUE,
                    intent_id TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    previous_digest TEXT NOT NULL,
                    digest TEXT NOT NULL UNIQUE
                )"""
            )
        finally:
            connection.close()

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _row(row: sqlite3.Row) -> RiskEvidenceRecord:
        return RiskEvidenceRecord(
            sequence=int(row["sequence"]),
            decision_id=str(row["decision_id"]),
            intent_id=str(row["intent_id"]),
            payload=dict(json.loads(str(row["payload"]))),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            previous_digest=str(row["previous_digest"]),
            digest=str(row["digest"]),
        )

    def append(
        self,
        *,
        decision_id: str,
        intent_id: str,
        payload: Mapping[str, object],
        created_at: datetime,
    ) -> RiskEvidenceRecord:
        if not decision_id.strip() or not intent_id.strip():
            raise ValueError("risk decision identity is required")
        moment = self._aware(created_at)
        normalized_payload = _normalize(payload)
        if not isinstance(normalized_payload, Mapping):
            raise TypeError("risk payload must normalize to an object")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM risk_decisions WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if existing is not None:
                record = self._row(existing)
                same_identity = record.decision_id == decision_id
                same_payload = dict(record.payload) == dict(normalized_payload)
                if not same_identity or not same_payload:
                    raise ValueError("RISK_DECISION_CONFLICT")
                connection.execute("COMMIT")
                return record
            tail = connection.execute(
                "SELECT sequence, digest FROM risk_decisions ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            sequence = 1 if tail is None else int(tail["sequence"]) + 1
            previous = ZERO_DIGEST if tail is None else str(tail["digest"])
            unsigned = {
                "sequence": sequence,
                "decision_id": decision_id,
                "intent_id": intent_id,
                "payload": dict(normalized_payload),
                "created_at": moment,
                "previous_digest": previous,
            }
            digest = sha256(unsigned)
            connection.execute(
                """INSERT INTO risk_decisions
                (sequence, decision_id, intent_id, payload, created_at, previous_digest, digest)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    sequence,
                    decision_id,
                    intent_id,
                    canonical_json(normalized_payload),
                    moment.isoformat(),
                    previous,
                    digest,
                ),
            )
            connection.execute("COMMIT")
            return RiskEvidenceRecord(
                sequence,
                decision_id,
                intent_id,
                dict(normalized_payload),
                moment,
                previous,
                digest,
            )
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def verify(self) -> tuple[RiskEvidenceRecord, ...]:
        connection = self._connect()
        try:
            rows = connection.execute("SELECT * FROM risk_decisions ORDER BY sequence").fetchall()
        finally:
            connection.close()
        records = tuple(self._row(row) for row in rows)
        previous = ZERO_DIGEST
        for expected_sequence, record in enumerate(records, start=1):
            if record.sequence != expected_sequence or record.previous_digest != previous:
                raise RuntimeError("RISK_EVIDENCE_CHAIN_BROKEN")
            if sha256(record.unsigned()) != record.digest:
                raise RuntimeError("RISK_EVIDENCE_DIGEST_MISMATCH")
            previous = record.digest
        return records


class RiskAdmissionService:
    """Evaluate pre-trade risk and persist the complete immutable decision evidence."""

    def __init__(self, *, engine: PreTradeRiskEngine, journal: RiskEvidenceJournal) -> None:
        self.engine = engine
        self.journal = journal

    def evaluate_and_record(
        self,
        intent: OrderIntent,
        *,
        current_symbol_notional: Decimal,
        current_gross_notional: Decimal,
        kill_switch_engaged: bool = False,
        context: RiskContext | None = None,
        evaluated_at: datetime,
    ) -> RecordedRiskDecision:
        if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
            raise ValueError("evaluated_at must be timezone-aware")
        decision = self.engine.evaluate(
            intent,
            current_symbol_notional=current_symbol_notional,
            current_gross_notional=current_gross_notional,
            kill_switch_engaged=kill_switch_engaged,
            context=context,
        )
        payload = {
            "intent": intent,
            "inputs": {
                "current_symbol_notional": current_symbol_notional,
                "current_gross_notional": current_gross_notional,
                "kill_switch_engaged": kill_switch_engaged,
                "context": context,
            },
            "limits": self.engine.limits,
            "decision": decision,
            "evaluated_at": evaluated_at,
        }
        decision_id = sha256(payload)
        record = self.journal.append(
            decision_id=decision_id,
            intent_id=intent.intent_id,
            payload=payload,
            created_at=evaluated_at,
        )
        return RecordedRiskDecision(
            decision=decision,
            decision_id=decision_id,
            evidence_digest=record.digest,
        )
