from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from app.risk.evidence import (
    ZERO_DIGEST,
    RiskEvidenceRecord,
    _normalize,
    canonical_json,
    sha256,
)

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None


class PostgresRiskEvidenceJournal:
    """Append-only hash-chained risk evidence with a locked PostgreSQL chain head."""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("dsn is required")
        if psycopg is None:
            raise RuntimeError("install the postgresql extra to use PostgresRiskEvidenceJournal")
        self.dsn = dsn

    def _connect(self):
        if psycopg is None or dict_row is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")
        return psycopg.connect(self.dsn, row_factory=dict_row, autocommit=False)

    def migrate(self, path: str | Path = "migrations/product/002_risk_evidence.sql") -> None:
        sql = Path(path).read_text(encoding="utf-8")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql)
            connection.commit()

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _row(row: Mapping[str, object]) -> RiskEvidenceRecord:
        created_at = row["created_at"]
        if not isinstance(created_at, datetime):
            created_at = datetime.fromisoformat(str(created_at))
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, Mapping):
            raise RuntimeError("invalid PostgreSQL risk evidence payload")
        return RiskEvidenceRecord(
            sequence=int(row["sequence"]),
            decision_id=str(row["decision_id"]),
            intent_id=str(row["intent_id"]),
            payload=dict(payload),
            created_at=created_at,
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
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT * FROM astra_risk_decisions WHERE intent_id=%s FOR UPDATE",
                        (intent_id,),
                    )
                    existing = cursor.fetchone()
                    if existing is not None:
                        record = self._row(existing)
                        if (
                            record.decision_id != decision_id
                            or dict(record.payload) != dict(normalized_payload)
                        ):
                            raise ValueError("RISK_DECISION_CONFLICT")
                        return record

                    cursor.execute(
                        """SELECT last_sequence, last_digest
                        FROM astra_risk_chain_state WHERE singleton=TRUE FOR UPDATE"""
                    )
                    state = cursor.fetchone()
                    if state is None:
                        raise RuntimeError("RISK_CHAIN_STATE_MISSING")
                    sequence = int(state["last_sequence"]) + 1
                    previous = str(state["last_digest"])
                    unsigned = {
                        "sequence": sequence,
                        "decision_id": decision_id,
                        "intent_id": intent_id,
                        "payload": dict(normalized_payload),
                        "created_at": moment,
                        "previous_digest": previous,
                    }
                    digest = sha256(unsigned)
                    cursor.execute(
                        """INSERT INTO astra_risk_decisions
                        (sequence, decision_id, intent_id, payload, created_at,
                         previous_digest, digest)
                        VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)""",
                        (
                            sequence,
                            decision_id,
                            intent_id,
                            canonical_json(normalized_payload),
                            moment,
                            previous,
                            digest,
                        ),
                    )
                    cursor.execute(
                        """UPDATE astra_risk_chain_state
                        SET last_sequence=%s, last_digest=%s WHERE singleton=TRUE""",
                        (sequence, digest),
                    )
                    return RiskEvidenceRecord(
                        sequence=sequence,
                        decision_id=decision_id,
                        intent_id=intent_id,
                        payload=dict(normalized_payload),
                        created_at=moment,
                        previous_digest=previous,
                        digest=digest,
                    )

    def verify(self) -> tuple[RiskEvidenceRecord, ...]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM astra_risk_decisions ORDER BY sequence")
                rows = cursor.fetchall()
        records = tuple(self._row(row) for row in rows)
        previous = ZERO_DIGEST
        for expected_sequence, record in enumerate(records, start=1):
            if record.sequence != expected_sequence or record.previous_digest != previous:
                raise RuntimeError("RISK_EVIDENCE_CHAIN_BROKEN")
            if sha256(record.unsigned()) != record.digest:
                raise RuntimeError("RISK_EVIDENCE_DIGEST_MISMATCH")
            previous = record.digest
        return records
