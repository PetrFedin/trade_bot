from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from app.qualification.qualification_manifest import QualificationManifest
from app.qualification.signed_evidence import (
    SignedQualificationEvidence,
    VerifiedQualificationEvidence,
)

_SCHEMA_VERSION = "astra-qualification-evidence-registry-v1"
_GENESIS = "0" * 64


class EvidenceLifecycleStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    REVOKED = "REVOKED"


class EvidenceRegistryEventType(StrEnum):
    REGISTERED = "REGISTERED"
    SUPERSEDED = "SUPERSEDED"
    REVOKED = "REVOKED"


class EvidenceVerificationStatus(StrEnum):
    VALID = "VALID"
    SUPERSEDED = "SUPERSEDED"
    REVOKED = "REVOKED"
    UNKNOWN = "UNKNOWN"
    INVALID = "INVALID"


@dataclass(frozen=True)
class EvidenceRegistryEvent:
    sequence: int
    event_type: EvidenceRegistryEventType
    evidence_id: str
    manifest_id: str
    manifest_sha256: str
    subject: str
    subject_version: str
    profile_id: str
    profile_version: str
    observed_at: datetime
    previous_event_sha256: str
    reason: str | None = None
    replacement_evidence_id: str | None = None
    schema_version: str = _SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError("qualification registry event schema mismatch")
        if self.sequence < 1:
            raise ValueError("qualification registry event sequence must be positive")
        for name, value in (
            ("evidence_id", self.evidence_id),
            ("manifest_id", self.manifest_id),
            ("subject", self.subject),
            ("subject_version", self.subject_version),
            ("profile_id", self.profile_id),
            ("profile_version", self.profile_version),
        ):
            if not value.strip():
                raise ValueError(f"{name} is required")
        _digest(self.manifest_sha256, "manifest_sha256")
        _digest(self.previous_event_sha256, "previous_event_sha256")
        _aware(self.observed_at, "observed_at")

        if self.event_type is EvidenceRegistryEventType.REGISTERED:
            if self.reason is not None or self.replacement_evidence_id is not None:
                raise ValueError("REGISTERED event cannot carry reason or replacement")
        elif self.event_type is EvidenceRegistryEventType.SUPERSEDED:
            if not (self.reason or "").strip():
                raise ValueError("SUPERSEDED event requires a reason")
            if not (self.replacement_evidence_id or "").strip():
                raise ValueError("SUPERSEDED event requires replacement evidence id")
            if self.replacement_evidence_id == self.evidence_id:
                raise ValueError("evidence cannot supersede itself")
        elif self.event_type is EvidenceRegistryEventType.REVOKED:
            if not (self.reason or "").strip():
                raise ValueError("REVOKED event requires a reason")
            if self.replacement_evidence_id is not None:
                raise ValueError("REVOKED event cannot carry replacement evidence id")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "event_type": self.event_type.value,
            "evidence_id": self.evidence_id,
            "manifest_id": self.manifest_id,
            "manifest_sha256": self.manifest_sha256,
            "subject": self.subject,
            "subject_version": self.subject_version,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "observed_at": _aware(self.observed_at, "observed_at").isoformat(),
            "previous_event_sha256": self.previous_event_sha256,
            "reason": self.reason,
            "replacement_evidence_id": self.replacement_evidence_id,
        }

    @property
    def event_sha256(self) -> str:
        return _sha256(self.payload())


@dataclass(frozen=True)
class EvidenceRegistryRecord:
    evidence_id: str
    manifest_id: str
    manifest_sha256: str
    subject: str
    subject_version: str
    profile_id: str
    profile_version: str
    status: EvidenceLifecycleStatus
    registered_at: datetime
    updated_at: datetime
    registration_event_sha256: str
    latest_event_sha256: str
    replacement_evidence_id: str | None = None
    lifecycle_reason: str | None = None

    def validate(self) -> None:
        for name, value in (
            ("evidence_id", self.evidence_id),
            ("manifest_id", self.manifest_id),
            ("subject", self.subject),
            ("subject_version", self.subject_version),
            ("profile_id", self.profile_id),
            ("profile_version", self.profile_version),
        ):
            if not value.strip():
                raise ValueError(f"{name} is required")
        for name, value in (
            ("manifest_sha256", self.manifest_sha256),
            ("registration_event_sha256", self.registration_event_sha256),
            ("latest_event_sha256", self.latest_event_sha256),
        ):
            _digest(value, name)
        registered = _aware(self.registered_at, "registered_at")
        updated = _aware(self.updated_at, "updated_at")
        if updated < registered:
            raise ValueError("qualification evidence updated_at cannot precede registered_at")
        if self.status is EvidenceLifecycleStatus.ACTIVE:
            if self.replacement_evidence_id is not None or self.lifecycle_reason is not None:
                raise ValueError("ACTIVE evidence cannot carry lifecycle closure")
        elif self.status is EvidenceLifecycleStatus.SUPERSEDED:
            if not (self.replacement_evidence_id or "").strip():
                raise ValueError("SUPERSEDED evidence requires replacement evidence id")
            if not (self.lifecycle_reason or "").strip():
                raise ValueError("SUPERSEDED evidence requires a reason")
        elif self.status is EvidenceLifecycleStatus.REVOKED:
            if self.replacement_evidence_id is not None:
                raise ValueError("REVOKED evidence cannot carry replacement evidence id")
            if not (self.lifecycle_reason or "").strip():
                raise ValueError("REVOKED evidence requires a reason")


@dataclass(frozen=True)
class EvidenceVerificationDecision:
    evidence_id: str
    status: EvidenceVerificationStatus
    manifest_id: str | None
    manifest_sha256: str | None
    subject: str | None
    subject_version: str | None
    profile_id: str | None
    profile_version: str | None
    replacement_evidence_id: str | None
    reason: str | None
    registry_head_sha256: str
    verified_at: datetime

    def validate(self) -> None:
        if not self.evidence_id.strip():
            raise ValueError("verification evidence_id is required")
        _digest(self.registry_head_sha256, "registry_head_sha256")
        _aware(self.verified_at, "verified_at")
        if self.status is EvidenceVerificationStatus.VALID and self.reason is not None:
            raise ValueError("VALID verification cannot carry a reason")
        if self.status is not EvidenceVerificationStatus.VALID and not (
            self.reason or ""
        ).strip():
            raise ValueError("non-VALID verification requires a reason")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "evidence_id": self.evidence_id,
            "status": self.status.value,
            "manifest_id": self.manifest_id,
            "manifest_sha256": self.manifest_sha256,
            "subject": self.subject,
            "subject_version": self.subject_version,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "replacement_evidence_id": self.replacement_evidence_id,
            "reason": self.reason,
            "registry_head_sha256": self.registry_head_sha256,
            "verified_at": _aware(self.verified_at, "verified_at").isoformat(),
        }


class InMemoryQualificationEvidenceRegistry:
    """Append-only qualification evidence lifecycle authority with a hash-chained event log."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[EvidenceRegistryEvent] = []
        self._records: dict[str, EvidenceRegistryRecord] = {}

    @property
    def head_sha256(self) -> str:
        with self._lock:
            return self._events[-1].event_sha256 if self._events else _GENESIS

    @property
    def event_count(self) -> int:
        with self._lock:
            return len(self._events)

    def register(
        self,
        *,
        manifest: QualificationManifest,
        signed_evidence: SignedQualificationEvidence,
        verification: VerifiedQualificationEvidence,
        observed_at: datetime,
    ) -> EvidenceRegistryRecord:
        manifest.validate()
        signed_evidence.validate()
        if verification.evidence_id != signed_evidence.evidence_id:
            raise ValueError("qualification registry verification evidence mismatch")
        if verification.manifest_id != manifest.manifest_id:
            raise ValueError("qualification registry verification manifest mismatch")
        if verification.manifest_sha256 != manifest.manifest_sha256:
            raise ValueError("qualification registry verification digest mismatch")
        if signed_evidence.manifest_id != manifest.manifest_id:
            raise ValueError("qualification registry signed manifest mismatch")
        if signed_evidence.manifest_sha256 != manifest.manifest_sha256:
            raise ValueError("qualification registry signed digest mismatch")

        now = _aware(observed_at, "observed_at")
        evidence_id = signed_evidence.evidence_id
        with self._lock:
            if evidence_id in self._records:
                raise ValueError("qualification evidence is already registered")
            previous = self._events[-1].event_sha256 if self._events else _GENESIS
            event = EvidenceRegistryEvent(
                sequence=len(self._events) + 1,
                event_type=EvidenceRegistryEventType.REGISTERED,
                evidence_id=evidence_id,
                manifest_id=manifest.manifest_id,
                manifest_sha256=manifest.manifest_sha256,
                subject=manifest.subject,
                subject_version=manifest.subject_version,
                profile_id=manifest.profile_id,
                profile_version=manifest.profile_version,
                observed_at=now,
                previous_event_sha256=previous,
            )
            event.validate()
            record = EvidenceRegistryRecord(
                evidence_id=evidence_id,
                manifest_id=manifest.manifest_id,
                manifest_sha256=manifest.manifest_sha256,
                subject=manifest.subject,
                subject_version=manifest.subject_version,
                profile_id=manifest.profile_id,
                profile_version=manifest.profile_version,
                status=EvidenceLifecycleStatus.ACTIVE,
                registered_at=now,
                updated_at=now,
                registration_event_sha256=event.event_sha256,
                latest_event_sha256=event.event_sha256,
            )
            record.validate()
            self._events.append(event)
            self._records[evidence_id] = record
            return record

    def supersede(
        self,
        *,
        evidence_id: str,
        replacement_evidence_id: str,
        reason: str,
        observed_at: datetime,
    ) -> EvidenceRegistryRecord:
        if not replacement_evidence_id.strip():
            raise ValueError("replacement_evidence_id is required")
        if not reason.strip():
            raise ValueError("supersession reason is required")
        now = _aware(observed_at, "observed_at")
        with self._lock:
            current = self._require_active(evidence_id)
            replacement = self._records.get(replacement_evidence_id)
            if replacement is None:
                raise ValueError("replacement qualification evidence is not registered")
            if replacement.status is not EvidenceLifecycleStatus.ACTIVE:
                raise ValueError("replacement qualification evidence must be ACTIVE")
            if (
                replacement.subject != current.subject
                or replacement.profile_id != current.profile_id
            ):
                raise ValueError("replacement qualification evidence scope mismatch")
            if replacement.registered_at < current.registered_at:
                raise ValueError("replacement qualification evidence predates current evidence")

            event = self._lifecycle_event(
                current=current,
                event_type=EvidenceRegistryEventType.SUPERSEDED,
                observed_at=now,
                reason=reason,
                replacement_evidence_id=replacement_evidence_id,
            )
            updated = EvidenceRegistryRecord(
                **{
                    **current.__dict__,
                    "status": EvidenceLifecycleStatus.SUPERSEDED,
                    "updated_at": now,
                    "latest_event_sha256": event.event_sha256,
                    "replacement_evidence_id": replacement_evidence_id,
                    "lifecycle_reason": reason,
                }
            )
            updated.validate()
            self._events.append(event)
            self._records[evidence_id] = updated
            return updated

    def revoke(
        self,
        *,
        evidence_id: str,
        reason: str,
        observed_at: datetime,
    ) -> EvidenceRegistryRecord:
        if not reason.strip():
            raise ValueError("revocation reason is required")
        now = _aware(observed_at, "observed_at")
        with self._lock:
            current = self._require_active(evidence_id)
            event = self._lifecycle_event(
                current=current,
                event_type=EvidenceRegistryEventType.REVOKED,
                observed_at=now,
                reason=reason,
                replacement_evidence_id=None,
            )
            updated = EvidenceRegistryRecord(
                **{
                    **current.__dict__,
                    "status": EvidenceLifecycleStatus.REVOKED,
                    "updated_at": now,
                    "latest_event_sha256": event.event_sha256,
                    "replacement_evidence_id": None,
                    "lifecycle_reason": reason,
                }
            )
            updated.validate()
            self._events.append(event)
            self._records[evidence_id] = updated
            return updated

    def verify(
        self,
        *,
        evidence_id: str,
        observed_at: datetime,
    ) -> EvidenceVerificationDecision:
        now = _aware(observed_at, "observed_at")
        with self._lock:
            record = self._records.get(evidence_id)
            head = self._events[-1].event_sha256 if self._events else _GENESIS
            if record is None:
                decision = EvidenceVerificationDecision(
                    evidence_id=evidence_id,
                    status=EvidenceVerificationStatus.UNKNOWN,
                    manifest_id=None,
                    manifest_sha256=None,
                    subject=None,
                    subject_version=None,
                    profile_id=None,
                    profile_version=None,
                    replacement_evidence_id=None,
                    reason="EVIDENCE_NOT_REGISTERED",
                    registry_head_sha256=head,
                    verified_at=now,
                )
            else:
                mapping = {
                    EvidenceLifecycleStatus.ACTIVE: EvidenceVerificationStatus.VALID,
                    EvidenceLifecycleStatus.SUPERSEDED: EvidenceVerificationStatus.SUPERSEDED,
                    EvidenceLifecycleStatus.REVOKED: EvidenceVerificationStatus.REVOKED,
                }
                decision = EvidenceVerificationDecision(
                    evidence_id=evidence_id,
                    status=mapping[record.status],
                    manifest_id=record.manifest_id,
                    manifest_sha256=record.manifest_sha256,
                    subject=record.subject,
                    subject_version=record.subject_version,
                    profile_id=record.profile_id,
                    profile_version=record.profile_version,
                    replacement_evidence_id=record.replacement_evidence_id,
                    reason=None if record.status is EvidenceLifecycleStatus.ACTIVE else record.lifecycle_reason,
                    registry_head_sha256=head,
                    verified_at=now,
                )
            decision.validate()
            return decision

    def events(self) -> tuple[EvidenceRegistryEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def verify_chain(self) -> str:
        with self._lock:
            previous = _GENESIS
            for expected_sequence, event in enumerate(self._events, start=1):
                event.validate()
                if event.sequence != expected_sequence:
                    raise ValueError("qualification registry event sequence mismatch")
                if event.previous_event_sha256 != previous:
                    raise ValueError("qualification registry event chain mismatch")
                previous = event.event_sha256
            return previous

    def _require_active(self, evidence_id: str) -> EvidenceRegistryRecord:
        current = self._records.get(evidence_id)
        if current is None:
            raise ValueError("qualification evidence is not registered")
        if current.status is not EvidenceLifecycleStatus.ACTIVE:
            raise ValueError("qualification evidence is not ACTIVE")
        return current

    def _lifecycle_event(
        self,
        *,
        current: EvidenceRegistryRecord,
        event_type: EvidenceRegistryEventType,
        observed_at: datetime,
        reason: str,
        replacement_evidence_id: str | None,
    ) -> EvidenceRegistryEvent:
        if observed_at < current.updated_at:
            raise ValueError("qualification evidence lifecycle time regression")
        previous = self._events[-1].event_sha256 if self._events else _GENESIS
        event = EvidenceRegistryEvent(
            sequence=len(self._events) + 1,
            event_type=event_type,
            evidence_id=current.evidence_id,
            manifest_id=current.manifest_id,
            manifest_sha256=current.manifest_sha256,
            subject=current.subject,
            subject_version=current.subject_version,
            profile_id=current.profile_id,
            profile_version=current.profile_version,
            observed_at=observed_at,
            previous_event_sha256=previous,
            reason=reason,
            replacement_evidence_id=replacement_evidence_id,
        )
        event.validate()
        return event


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
