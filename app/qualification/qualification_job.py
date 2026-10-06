from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from app.qualification.adapter_conformance import AdapterConformanceReport
from app.qualification.marketdata_integrity import (
    MarketDataIntegrityDecision,
    MarketDataIntegrityStatus,
)

_SCHEMA_VERSION = "astra-qualification-job-v1"


class QualificationJobStatus(StrEnum):
    PASS = "PASS"  # nosec B105 - qualification result label, not a credential
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class QualificationJobRequest:
    organisation_id: str
    subject: str
    subject_version: str
    profile_id: str
    profile_version: str
    environment: str
    corpus_ids: tuple[str, ...]
    submitted_at: datetime

    def validate(self) -> None:
        for name, value in (
            ("organisation_id", self.organisation_id),
            ("subject", self.subject),
            ("subject_version", self.subject_version),
            ("profile_id", self.profile_id),
            ("profile_version", self.profile_version),
            ("environment", self.environment),
        ):
            if not value.strip():
                raise ValueError(f"{name} is required")
        _aware(self.submitted_at, "submitted_at")
        if not self.corpus_ids:
            raise ValueError("qualification job requires at least one corpus")
        if len(set(self.corpus_ids)) != len(self.corpus_ids):
            raise ValueError("qualification corpus IDs must be unique")
        if any(not item.strip() for item in self.corpus_ids):
            raise ValueError("qualification corpus IDs cannot be blank")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "organisation_id": self.organisation_id,
            "subject": self.subject,
            "subject_version": self.subject_version,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "environment": self.environment,
            "corpus_ids": list(self.corpus_ids),
            "submitted_at": _aware(self.submitted_at, "submitted_at").isoformat(),
        }

    @property
    def request_sha256(self) -> str:
        return _sha256(self.payload())

    @property
    def job_id(self) -> str:
        return f"qjob_{self.request_sha256[:24]}"


@dataclass(frozen=True)
class QualificationJobResult:
    request: QualificationJobRequest
    status: QualificationJobStatus
    reasons: tuple[str, ...]
    started_at: datetime
    completed_at: datetime
    adapter_conformance_sha256: str
    marketdata_integrity_sha256: str
    schema_version: str = _SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError("qualification job schema mismatch")
        self.request.validate()
        started = _aware(self.started_at, "started_at")
        completed = _aware(self.completed_at, "completed_at")
        if started < self.request.submitted_at.astimezone(UTC):
            raise ValueError("qualification job cannot start before submission")
        if completed < started:
            raise ValueError("qualification job cannot complete before it starts")
        _digest(self.adapter_conformance_sha256, "adapter_conformance_sha256")
        _digest(self.marketdata_integrity_sha256, "marketdata_integrity_sha256")
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError("qualification job reasons must be unique")
        if any(not reason.strip() for reason in self.reasons):
            raise ValueError("qualification job reasons cannot be blank")
        if self.status is QualificationJobStatus.PASS and self.reasons:
            raise ValueError("passing qualification job cannot carry failure reasons")
        if self.status is not QualificationJobStatus.PASS and not self.reasons:
            raise ValueError("failed or blocked qualification job requires reasons")

    @property
    def job_id(self) -> str:
        return self.request.job_id

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "request": self.request.payload(),
            "status": self.status.value,
            "reasons": list(self.reasons),
            "started_at": _aware(self.started_at, "started_at").isoformat(),
            "completed_at": _aware(self.completed_at, "completed_at").isoformat(),
            "adapter_conformance_sha256": self.adapter_conformance_sha256,
            "marketdata_integrity_sha256": self.marketdata_integrity_sha256,
        }

    @property
    def result_sha256(self) -> str:
        return _sha256(self.payload())


def evaluate_public_marketdata_qualification_job(
    *,
    request: QualificationJobRequest,
    adapter_conformance: AdapterConformanceReport,
    marketdata_integrity: MarketDataIntegrityDecision,
    started_at: datetime,
    completed_at: datetime,
) -> QualificationJobResult:
    """Evaluate a completed qualification job from already-produced evidence.

    This authority cannot run arbitrary partner code and cannot authorize trading.
    It only binds a request to exact qualification evidence and produces PASS/FAIL/BLOCKED.
    """

    request.validate()
    structural_errors: list[str] = []
    for name, value in (
        ("adapter_conformance", adapter_conformance),
        ("marketdata_integrity", marketdata_integrity),
    ):
        try:
            value.validate()
        except Exception as exc:
            structural_errors.append(f"{name}:{type(exc).__name__}:{exc}")

    reasons: set[str] = set()
    status = QualificationJobStatus.PASS

    if structural_errors:
        status = QualificationJobStatus.BLOCKED
        reasons.add("STRUCTURAL_EVIDENCE_INVALID")
    else:
        if adapter_conformance.profile_id != request.profile_id:
            reasons.add("PROFILE_ID_MISMATCH")
        if adapter_conformance.profile_version != request.profile_version:
            reasons.add("PROFILE_VERSION_MISMATCH")
        if adapter_conformance.subject != request.subject:
            reasons.add("SUBJECT_MISMATCH")
        if adapter_conformance.subject_version != request.subject_version:
            reasons.add("SUBJECT_VERSION_MISMATCH")
        if (
            marketdata_integrity.adapter_conformance_sha256
            != adapter_conformance.evidence_sha256
        ):
            reasons.add("EVIDENCE_CHAIN_MISMATCH")

        if reasons:
            status = QualificationJobStatus.BLOCKED
        elif not adapter_conformance.qualified:
            status = QualificationJobStatus.FAIL
            reasons.add("ADAPTER_CONFORMANCE_FAILED")
        elif marketdata_integrity.status is not MarketDataIntegrityStatus.HEALTHY:
            status = QualificationJobStatus.FAIL
            reasons.add(
                f"MARKET_DATA_INTEGRITY_{marketdata_integrity.status.value}"
            )

    result = QualificationJobResult(
        request=request,
        status=status,
        reasons=tuple(sorted(reasons)),
        started_at=started_at,
        completed_at=completed_at,
        adapter_conformance_sha256=adapter_conformance.evidence_sha256,
        marketdata_integrity_sha256=marketdata_integrity.evidence_sha256,
    )
    result.validate()
    return result


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


def _digest(value: str, name: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{name} must be a sha256 digest")
    return normalized
