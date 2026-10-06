from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

_JOB_SCHEMA = "astra-qualification-job-v1"
_EVIDENCE_SCHEMA = "astra-qualification-evidence-bundle-v1"
_ALLOWED_ENVIRONMENTS = {
    "REPLAY",
    "PARTNER_SANDBOX",
    "PAPER_READONLY",
}


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _required(value: str, name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} is required")
    return normalized


def _sha256_text(value: str, name: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{name} must be a sha256 digest")
    return normalized


def _source_revision(value: str, name: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) not in {40, 64} or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{name} must be a 40- or 64-character hexadecimal revision")
    return normalized


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class QualificationJobStatus(StrEnum):
    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"


_TERMINAL_STATUSES = {
    QualificationJobStatus.PASSED,
    QualificationJobStatus.FAILED,
    QualificationJobStatus.REJECTED,
}

_ALLOWED_TRANSITIONS = {
    QualificationJobStatus.SUBMITTED: {
        QualificationJobStatus.RUNNING,
        QualificationJobStatus.REJECTED,
    },
    QualificationJobStatus.RUNNING: _TERMINAL_STATUSES,
}


@dataclass(frozen=True)
class QualificationJobSpec:
    organization_id: str
    submitter_id: str
    subject: str
    subject_version: str
    artifact_sha256: str
    profile_id: str
    profile_version: str
    corpus_id: str
    corpus_version: str
    replay_evidence_sha256: str
    environment: str
    expected_capabilities: tuple[str, ...]
    astra_release_sha: str
    requested_at: datetime
    schema_version: str = _JOB_SCHEMA

    def validate(self) -> None:
        if self.schema_version != _JOB_SCHEMA:
            raise ValueError("qualification job schema mismatch")
        for name, value in (
            ("organization_id", self.organization_id),
            ("submitter_id", self.submitter_id),
            ("subject", self.subject),
            ("subject_version", self.subject_version),
            ("profile_id", self.profile_id),
            ("profile_version", self.profile_version),
            ("corpus_id", self.corpus_id),
            ("corpus_version", self.corpus_version),
        ):
            _required(value, name)
        _sha256_text(self.artifact_sha256, "artifact_sha256")
        _sha256_text(self.replay_evidence_sha256, "replay_evidence_sha256")
        _source_revision(self.astra_release_sha, "astra_release_sha")
        _aware(self.requested_at, "requested_at")

        if self.environment not in _ALLOWED_ENVIRONMENTS:
            raise ValueError("qualification environment is not non-live allowlisted")
        if not self.expected_capabilities:
            raise ValueError("expected_capabilities are required")
        if tuple(sorted(set(self.expected_capabilities))) != self.expected_capabilities:
            raise ValueError(
                "expected_capabilities must be unique and canonically sorted"
            )
        for capability in self.expected_capabilities:
            if (
                not capability
                or capability != capability.strip().upper()
                or any(character.isspace() for character in capability)
            ):
                raise ValueError(
                    "expected_capabilities must contain normalized uppercase identifiers"
                )

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "organization_id": self.organization_id,
            "submitter_id": self.submitter_id,
            "subject": self.subject,
            "subject_version": self.subject_version,
            "artifact_sha256": self.artifact_sha256.lower(),
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "corpus_id": self.corpus_id,
            "corpus_version": self.corpus_version,
            "replay_evidence_sha256": self.replay_evidence_sha256.lower(),
            "environment": self.environment,
            "expected_capabilities": list(self.expected_capabilities),
            "astra_release_sha": self.astra_release_sha.lower(),
            "requested_at": _aware(self.requested_at, "requested_at").isoformat(),
        }

    @property
    def job_id(self) -> str:
        return canonical_sha256(self.payload())

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> QualificationJobSpec:
        capabilities = payload.get("expected_capabilities")
        if not isinstance(capabilities, list) or any(
            not isinstance(item, str) for item in capabilities
        ):
            raise ValueError("qualification job capabilities payload is invalid")
        requested_at = payload.get("requested_at")
        if not isinstance(requested_at, str):
            raise ValueError("qualification job requested_at payload is invalid")
        value = cls(
            organization_id=str(payload.get("organization_id", "")),
            submitter_id=str(payload.get("submitter_id", "")),
            subject=str(payload.get("subject", "")),
            subject_version=str(payload.get("subject_version", "")),
            artifact_sha256=str(payload.get("artifact_sha256", "")),
            profile_id=str(payload.get("profile_id", "")),
            profile_version=str(payload.get("profile_version", "")),
            corpus_id=str(payload.get("corpus_id", "")),
            corpus_version=str(payload.get("corpus_version", "")),
            replay_evidence_sha256=str(payload.get("replay_evidence_sha256", "")),
            environment=str(payload.get("environment", "")),
            expected_capabilities=tuple(capabilities),
            astra_release_sha=str(payload.get("astra_release_sha", "")),
            requested_at=datetime.fromisoformat(requested_at),
            schema_version=str(payload.get("schema_version", "")),
        )
        value.validate()
        return value


@dataclass(frozen=True)
class QualificationEvidenceBundle:
    provider_replay_sha256: str
    adapter_conformance_sha256: str
    market_integrity_sha256: str
    additional_evidence_sha256: tuple[str, ...] = ()
    schema_version: str = _EVIDENCE_SCHEMA

    def validate(self) -> None:
        if self.schema_version != _EVIDENCE_SCHEMA:
            raise ValueError("qualification evidence bundle schema mismatch")
        _sha256_text(self.provider_replay_sha256, "provider_replay_sha256")
        _sha256_text(
            self.adapter_conformance_sha256,
            "adapter_conformance_sha256",
        )
        _sha256_text(self.market_integrity_sha256, "market_integrity_sha256")
        if tuple(sorted(set(self.additional_evidence_sha256))) != (
            self.additional_evidence_sha256
        ):
            raise ValueError(
                "additional evidence digests must be unique and canonically sorted"
            )
        for digest in self.additional_evidence_sha256:
            _sha256_text(digest, "additional_evidence_sha256")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "provider_replay_sha256": self.provider_replay_sha256.lower(),
            "adapter_conformance_sha256": self.adapter_conformance_sha256.lower(),
            "market_integrity_sha256": self.market_integrity_sha256.lower(),
            "additional_evidence_sha256": list(self.additional_evidence_sha256),
        }

    @property
    def evidence_sha256(self) -> str:
        return canonical_sha256(self.payload())


@dataclass(frozen=True)
class QualificationJobEvent:
    sequence: int
    event_id: str
    job_id: str
    status: QualificationJobStatus
    occurred_at: datetime
    evidence_sha256: str | None
    reasons: tuple[str, ...]

    def validate(self) -> None:
        if self.sequence < 1:
            raise ValueError("qualification event sequence must be positive")
        _sha256_text(self.event_id, "event_id")
        _sha256_text(self.job_id, "job_id")
        _aware(self.occurred_at, "occurred_at")
        if self.evidence_sha256 is not None:
            _sha256_text(self.evidence_sha256, "evidence_sha256")
        if tuple(sorted(set(self.reasons))) != self.reasons:
            raise ValueError("qualification event reasons must be unique and sorted")
        if any(not reason.strip() for reason in self.reasons):
            raise ValueError("qualification event reasons cannot be blank")

        if self.status in {
            QualificationJobStatus.SUBMITTED,
            QualificationJobStatus.RUNNING,
        }:
            if self.evidence_sha256 is not None or self.reasons:
                raise ValueError(
                    "non-terminal qualification events cannot contain result evidence"
                )
        elif self.status is QualificationJobStatus.PASSED:
            if self.evidence_sha256 is None or self.reasons:
                raise ValueError("PASSED requires evidence and no failure reasons")
        elif self.status is QualificationJobStatus.FAILED:
            if self.evidence_sha256 is None or not self.reasons:
                raise ValueError("FAILED requires evidence and failure reasons")
        elif self.status is QualificationJobStatus.REJECTED and not self.reasons:
            raise ValueError("REJECTED requires reasons")


@dataclass(frozen=True)
class QualificationJobRecord:
    spec: QualificationJobSpec
    events: tuple[QualificationJobEvent, ...]

    def validate(self) -> None:
        self.spec.validate()
        if not self.events:
            raise ValueError("qualification job requires events")
        if self.events[0].status is not QualificationJobStatus.SUBMITTED:
            raise ValueError("qualification job must begin SUBMITTED")
        previous_status: QualificationJobStatus | None = None
        previous_time: datetime | None = None
        for expected_sequence, event in enumerate(self.events, start=1):
            event.validate()
            if event.sequence != expected_sequence:
                raise ValueError("qualification event sequence is not contiguous")
            if event.job_id != self.spec.job_id:
                raise ValueError("qualification event job identity mismatch")
            if previous_time is not None and event.occurred_at < previous_time:
                raise ValueError("qualification event time moved backward")
            if previous_status is not None:
                allowed = _ALLOWED_TRANSITIONS.get(previous_status, set())
                if event.status not in allowed:
                    raise ValueError("qualification event transition is invalid")
            previous_status = event.status
            previous_time = event.occurred_at

    @property
    def status(self) -> QualificationJobStatus:
        self.validate()
        return self.events[-1].status

    @property
    def terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES


class QualificationJobStore(Protocol):
    def submit(self, spec: QualificationJobSpec) -> QualificationJobRecord: ...

    def transition(
        self,
        job_id: str,
        *,
        status: QualificationJobStatus,
        occurred_at: datetime,
        evidence_sha256: str | None = None,
        reasons: tuple[str, ...] = (),
    ) -> QualificationJobRecord: ...

    def get(self, job_id: str) -> QualificationJobRecord | None: ...


class SQLiteQualificationJobStore:
    """Durable append-only qualification job specification and lifecycle journal."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            isolation_level=None,
            timeout=10,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
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
                CREATE TABLE IF NOT EXISTS qualification_jobs (
                    job_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    requested_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS qualification_job_events (
                    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sequence INTEGER NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    job_id TEXT NOT NULL REFERENCES qualification_jobs(job_id),
                    status TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    evidence_sha256 TEXT,
                    reasons TEXT NOT NULL,
                    UNIQUE(job_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS idx_qualification_job_events_job
                ON qualification_job_events(job_id, sequence);

                CREATE TRIGGER IF NOT EXISTS qualification_jobs_no_update
                BEFORE UPDATE ON qualification_jobs BEGIN
                    SELECT RAISE(ABORT, 'qualification_jobs is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS qualification_jobs_no_delete
                BEFORE DELETE ON qualification_jobs BEGIN
                    SELECT RAISE(ABORT, 'qualification_jobs is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS qualification_job_events_no_update
                BEFORE UPDATE ON qualification_job_events BEGIN
                    SELECT RAISE(ABORT, 'qualification_job_events is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS qualification_job_events_no_delete
                BEFORE DELETE ON qualification_job_events BEGIN
                    SELECT RAISE(ABORT, 'qualification_job_events is append-only');
                END;
                """
            )
        finally:
            connection.close()

    @staticmethod
    def _event_id(
        *,
        job_id: str,
        status: QualificationJobStatus,
        occurred_at: datetime,
        evidence_sha256: str | None,
        reasons: tuple[str, ...],
    ) -> str:
        return canonical_sha256(
            {
                "job_id": job_id,
                "status": status.value,
                "occurred_at": _aware(occurred_at, "occurred_at").isoformat(),
                "evidence_sha256": evidence_sha256,
                "reasons": list(reasons),
            }
        )

    @staticmethod
    def _event(row: sqlite3.Row) -> QualificationJobEvent:
        reasons = json.loads(str(row["reasons"]))
        if not isinstance(reasons, list) or any(
            not isinstance(item, str) for item in reasons
        ):
            raise RuntimeError("qualification event reasons payload is corrupt")
        event = QualificationJobEvent(
            sequence=int(row["sequence"]),
            event_id=str(row["event_id"]),
            job_id=str(row["job_id"]),
            status=QualificationJobStatus(str(row["status"])),
            occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
            evidence_sha256=(
                None
                if row["evidence_sha256"] is None
                else str(row["evidence_sha256"])
            ),
            reasons=tuple(reasons),
        )
        event.validate()
        return event

    def _record(
        self,
        connection: sqlite3.Connection,
        job_id: str,
    ) -> QualificationJobRecord:
        job = connection.execute(
            "SELECT payload FROM qualification_jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if job is None:
            raise KeyError(f"qualification job not found: {job_id}")
        payload = json.loads(str(job["payload"]))
        if not isinstance(payload, dict):
            raise RuntimeError("qualification job payload is corrupt")
        rows = connection.execute(
            """
            SELECT sequence, event_id, job_id, status, occurred_at,
                   evidence_sha256, reasons
            FROM qualification_job_events
            WHERE job_id=?
            ORDER BY sequence
            """,
            (job_id,),
        ).fetchall()
        record = QualificationJobRecord(
            spec=QualificationJobSpec.from_payload(payload),
            events=tuple(self._event(row) for row in rows),
        )
        record.validate()
        return record

    def submit(self, spec: QualificationJobSpec) -> QualificationJobRecord:
        spec.validate()
        payload = _canonical_json(spec.payload())
        job_id = spec.job_id
        moment = _aware(spec.requested_at, "requested_at")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload FROM qualification_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO qualification_jobs(job_id, payload, requested_at)
                    VALUES (?, ?, ?)
                    """,
                    (job_id, payload, moment.isoformat()),
                )
                event_id = self._event_id(
                    job_id=job_id,
                    status=QualificationJobStatus.SUBMITTED,
                    occurred_at=moment,
                    evidence_sha256=None,
                    reasons=(),
                )
                connection.execute(
                    """
                    INSERT INTO qualification_job_events(
                        sequence, event_id, job_id, status, occurred_at,
                        evidence_sha256, reasons
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        1,
                        event_id,
                        job_id,
                        QualificationJobStatus.SUBMITTED.value,
                        moment.isoformat(),
                        None,
                        "[]",
                    ),
                )
            elif str(existing["payload"]) != payload:
                raise ValueError("QUALIFICATION_JOB_ID_CONFLICT")
            record = self._record(connection, job_id)
            connection.execute("COMMIT")
            return record
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        finally:
            connection.close()

    def transition(
        self,
        job_id: str,
        *,
        status: QualificationJobStatus,
        occurred_at: datetime,
        evidence_sha256: str | None = None,
        reasons: tuple[str, ...] = (),
    ) -> QualificationJobRecord:
        _sha256_text(job_id, "job_id")
        status = QualificationJobStatus(status)
        moment = _aware(occurred_at, "occurred_at")
        canonical_reasons = tuple(sorted(set(reasons)))
        candidate_id = self._event_id(
            job_id=job_id,
            status=status,
            occurred_at=moment,
            evidence_sha256=evidence_sha256,
            reasons=canonical_reasons,
        )
        candidate = QualificationJobEvent(
            sequence=1,
            event_id=candidate_id,
            job_id=job_id,
            status=status,
            occurred_at=moment,
            evidence_sha256=evidence_sha256,
            reasons=canonical_reasons,
        )
        candidate.validate()

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            record = self._record(connection, job_id)
            current = record.events[-1]
            if current.status in _TERMINAL_STATUSES:
                if (
                    current.status is status
                    and current.occurred_at == moment
                    and current.evidence_sha256 == evidence_sha256
                    and current.reasons == canonical_reasons
                ):
                    connection.execute("COMMIT")
                    return record
                raise ValueError("QUALIFICATION_JOB_ALREADY_TERMINAL")

            allowed = _ALLOWED_TRANSITIONS.get(current.status, set())
            if status not in allowed:
                raise ValueError("QUALIFICATION_JOB_TRANSITION_INVALID")
            if moment < current.occurred_at:
                raise ValueError("QUALIFICATION_JOB_TIME_REGRESSION")

            connection.execute(
                """
                INSERT INTO qualification_job_events(
                    sequence, event_id, job_id, status, occurred_at,
                    evidence_sha256, reasons
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    current.sequence + 1,
                    candidate_id,
                    job_id,
                    status.value,
                    moment.isoformat(),
                    evidence_sha256,
                    _canonical_json(list(canonical_reasons)),
                ),
            )
            updated = self._record(connection, job_id)
            connection.execute("COMMIT")
            return updated
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise
        finally:
            connection.close()

    def get(self, job_id: str) -> QualificationJobRecord | None:
        _sha256_text(job_id, "job_id")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT 1 FROM qualification_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if row is None:
                return None
            return self._record(connection, job_id)
        finally:
            connection.close()


class QualificationJobAuthority:
    """State-machine facade; it cannot execute adapters or place orders."""

    def __init__(self, store: QualificationJobStore) -> None:
        self.store = store

    def submit(self, spec: QualificationJobSpec) -> QualificationJobRecord:
        return self.store.submit(spec)

    def start(
        self,
        job_id: str,
        *,
        occurred_at: datetime,
    ) -> QualificationJobRecord:
        return self.store.transition(
            job_id,
            status=QualificationJobStatus.RUNNING,
            occurred_at=occurred_at,
        )

    def complete(
        self,
        job_id: str,
        *,
        status: QualificationJobStatus,
        evidence: QualificationEvidenceBundle,
        occurred_at: datetime,
        reasons: tuple[str, ...] = (),
    ) -> QualificationJobRecord:
        if status not in _TERMINAL_STATUSES:
            raise ValueError("qualification completion status must be terminal")
        evidence.validate()
        return self.store.transition(
            job_id,
            status=status,
            occurred_at=occurred_at,
            evidence_sha256=evidence.evidence_sha256,
            reasons=reasons,
        )
