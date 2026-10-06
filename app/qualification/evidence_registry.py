from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from app.qualification.profile_binding import ProfileBoundQualificationManifest
from app.qualification.qualification_manifest import QualificationManifest
from app.qualification.signed_evidence import (
    SignedQualificationEvidence,
    verify_qualification_evidence,
)
from app.qualification.signing_authority import (
    QualificationSignatureReplayLedger,
    VerifiedQualificationKeyring,
)

_SCHEMA_VERSION = "astra-profile-bound-evidence-registry-v2"
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


@dataclass(frozen=True)
class EvidenceRegistryEvent:
    sequence: int
    event_type: EvidenceRegistryEventType
    evidence_id: str
    binding_id: str
    binding_sha256: str
    manifest_id: str
    manifest_sha256: str
    profile_sha256: str
    subject: str
    subject_version: str
    profile_id: str
    profile_version: str
    signer_key_id: str
    signer_owner_id: str
    signer_key_generation: int
    keyring_generation: int
    signature_envelope_sha256: str
    cryptographically_verified_at: datetime
    observed_at: datetime
    previous_event_sha256: str
    reason: str | None = None
    replacement_evidence_id: str | None = None
    schema_version: str = _SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError("qualification evidence registry schema mismatch")
        if self.sequence < 1:
            raise ValueError("qualification registry sequence must be positive")
        for name, value in (
            ("evidence_id", self.evidence_id),
            ("binding_id", self.binding_id),
            ("manifest_id", self.manifest_id),
            ("subject", self.subject),
            ("subject_version", self.subject_version),
            ("profile_id", self.profile_id),
            ("profile_version", self.profile_version),
            ("signer_key_id", self.signer_key_id),
            ("signer_owner_id", self.signer_owner_id),
        ):
            if not value.strip():
                raise ValueError(f"{name} is required")
        for name, value in (
            ("binding_sha256", self.binding_sha256),
            ("manifest_sha256", self.manifest_sha256),
            ("profile_sha256", self.profile_sha256),
            ("signature_envelope_sha256", self.signature_envelope_sha256),
            ("previous_event_sha256", self.previous_event_sha256),
        ):
            _digest(value, name)
        if self.signer_key_generation < 1 or self.keyring_generation < 1:
            raise ValueError("qualification registry signer generations must be positive")
        verified = _aware(
            self.cryptographically_verified_at,
            "cryptographically_verified_at",
        )
        observed = _aware(self.observed_at, "observed_at")
        if observed < verified:
            raise ValueError("registry event cannot predate cryptographic verification")

        if self.event_type is EvidenceRegistryEventType.REGISTERED:
            if self.reason is not None or self.replacement_evidence_id is not None:
                raise ValueError("REGISTERED event cannot carry lifecycle closure")
        elif self.event_type is EvidenceRegistryEventType.SUPERSEDED:
            if not (self.reason or "").strip():
                raise ValueError("SUPERSEDED event requires a reason")
            if not (self.replacement_evidence_id or "").strip():
                raise ValueError("SUPERSEDED event requires replacement evidence")
            if self.replacement_evidence_id == self.evidence_id:
                raise ValueError("qualification evidence cannot supersede itself")
        elif self.event_type is EvidenceRegistryEventType.REVOKED:
            if not (self.reason or "").strip():
                raise ValueError("REVOKED event requires a reason")
            if self.replacement_evidence_id is not None:
                raise ValueError("REVOKED event cannot carry replacement evidence")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "event_type": self.event_type.value,
            "evidence_id": self.evidence_id,
            "binding_id": self.binding_id,
            "binding_sha256": self.binding_sha256,
            "manifest_id": self.manifest_id,
            "manifest_sha256": self.manifest_sha256,
            "profile_sha256": self.profile_sha256,
            "subject": self.subject,
            "subject_version": self.subject_version,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "signer_key_id": self.signer_key_id,
            "signer_owner_id": self.signer_owner_id,
            "signer_key_generation": self.signer_key_generation,
            "keyring_generation": self.keyring_generation,
            "signature_envelope_sha256": self.signature_envelope_sha256,
            "cryptographically_verified_at": _aware(
                self.cryptographically_verified_at,
                "cryptographically_verified_at",
            ).isoformat(),
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
    binding_id: str
    binding_sha256: str
    manifest_id: str
    manifest_sha256: str
    profile_sha256: str
    subject: str
    subject_version: str
    profile_id: str
    profile_version: str
    signer_key_id: str
    signer_owner_id: str
    signer_key_generation: int
    keyring_generation: int
    signature_envelope_sha256: str
    cryptographically_verified_at: datetime
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
            ("binding_id", self.binding_id),
            ("manifest_id", self.manifest_id),
            ("subject", self.subject),
            ("subject_version", self.subject_version),
            ("profile_id", self.profile_id),
            ("profile_version", self.profile_version),
            ("signer_key_id", self.signer_key_id),
            ("signer_owner_id", self.signer_owner_id),
        ):
            if not value.strip():
                raise ValueError(f"{name} is required")
        for name, value in (
            ("binding_sha256", self.binding_sha256),
            ("manifest_sha256", self.manifest_sha256),
            ("profile_sha256", self.profile_sha256),
            ("signature_envelope_sha256", self.signature_envelope_sha256),
            ("registration_event_sha256", self.registration_event_sha256),
            ("latest_event_sha256", self.latest_event_sha256),
        ):
            _digest(value, name)
        if self.signer_key_generation < 1 or self.keyring_generation < 1:
            raise ValueError("qualification registry signer generations must be positive")
        verified = _aware(
            self.cryptographically_verified_at,
            "cryptographically_verified_at",
        )
        registered = _aware(self.registered_at, "registered_at")
        updated = _aware(self.updated_at, "updated_at")
        if registered < verified:
            raise ValueError("qualification evidence registration predates verification")
        if updated < registered:
            raise ValueError("qualification evidence updated_at cannot regress")
        if self.status is EvidenceLifecycleStatus.ACTIVE:
            if self.replacement_evidence_id is not None or self.lifecycle_reason is not None:
                raise ValueError("ACTIVE evidence cannot carry lifecycle closure")
        elif self.status is EvidenceLifecycleStatus.SUPERSEDED:
            if not (self.replacement_evidence_id or "").strip():
                raise ValueError("SUPERSEDED evidence requires replacement")
            if not (self.lifecycle_reason or "").strip():
                raise ValueError("SUPERSEDED evidence requires reason")
        elif self.status is EvidenceLifecycleStatus.REVOKED:
            if self.replacement_evidence_id is not None:
                raise ValueError("REVOKED evidence cannot carry replacement")
            if not (self.lifecycle_reason or "").strip():
                raise ValueError("REVOKED evidence requires reason")

    def state_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "evidence_id": self.evidence_id,
            "binding_id": self.binding_id,
            "binding_sha256": self.binding_sha256,
            "manifest_id": self.manifest_id,
            "manifest_sha256": self.manifest_sha256,
            "profile_sha256": self.profile_sha256,
            "subject": self.subject,
            "subject_version": self.subject_version,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "signer_key_id": self.signer_key_id,
            "signer_owner_id": self.signer_owner_id,
            "signer_key_generation": self.signer_key_generation,
            "keyring_generation": self.keyring_generation,
            "signature_envelope_sha256": self.signature_envelope_sha256,
            "cryptographically_verified_at": _aware(
                self.cryptographically_verified_at,
                "cryptographically_verified_at",
            ).isoformat(),
            "status": self.status.value,
            "registered_at": _aware(self.registered_at, "registered_at").isoformat(),
            "updated_at": _aware(self.updated_at, "updated_at").isoformat(),
            "registration_event_sha256": self.registration_event_sha256,
            "latest_event_sha256": self.latest_event_sha256,
            "replacement_evidence_id": self.replacement_evidence_id,
            "lifecycle_reason": self.lifecycle_reason,
        }

    @property
    def state_leaf_sha256(self) -> str:
        return _leaf_hash(_sha256(self.state_payload()))


@dataclass(frozen=True)
class EvidenceStateProof:
    evidence_id: str
    record: EvidenceRegistryRecord
    leaf_index: int
    tree_size: int
    audit_path: tuple[str, ...]
    state_root_sha256: str

    def validate(self) -> None:
        if self.evidence_id != self.record.evidence_id:
            raise ValueError("evidence state proof identity mismatch")
        if self.tree_size < 1:
            raise ValueError("evidence state proof tree_size must be positive")
        if self.leaf_index < 0 or self.leaf_index >= self.tree_size:
            raise ValueError("evidence state proof leaf_index is out of range")
        self.record.validate()
        _digest(self.state_root_sha256, "state_root_sha256")
        for item in self.audit_path:
            _digest(item, "audit_path item")


@dataclass(frozen=True)
class EvidenceVerificationDecision:
    evidence_id: str
    status: EvidenceVerificationStatus
    binding_id: str | None
    binding_sha256: str | None
    manifest_id: str | None
    manifest_sha256: str | None
    profile_sha256: str | None
    signer_key_id: str | None
    signer_owner_id: str | None
    signer_key_generation: int | None
    keyring_generation: int | None
    signature_envelope_sha256: str | None
    replacement_evidence_id: str | None
    reason: str | None
    registry_head_sha256: str
    state_root_sha256: str
    verified_at: datetime

    def validate(self) -> None:
        if not self.evidence_id.strip():
            raise ValueError("verification evidence_id is required")
        _digest(self.registry_head_sha256, "registry_head_sha256")
        _digest(self.state_root_sha256, "state_root_sha256")
        _aware(self.verified_at, "verified_at")
        if self.status is EvidenceVerificationStatus.VALID and self.reason is not None:
            raise ValueError("VALID evidence cannot carry reason")
        if self.status is not EvidenceVerificationStatus.VALID and not (
            self.reason or ""
        ).strip():
            raise ValueError("non-VALID evidence requires reason")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "evidence_id": self.evidence_id,
            "status": self.status.value,
            "binding_id": self.binding_id,
            "binding_sha256": self.binding_sha256,
            "manifest_id": self.manifest_id,
            "manifest_sha256": self.manifest_sha256,
            "profile_sha256": self.profile_sha256,
            "signer_key_id": self.signer_key_id,
            "signer_owner_id": self.signer_owner_id,
            "signer_key_generation": self.signer_key_generation,
            "keyring_generation": self.keyring_generation,
            "signature_envelope_sha256": self.signature_envelope_sha256,
            "replacement_evidence_id": self.replacement_evidence_id,
            "reason": self.reason,
            "registry_head_sha256": self.registry_head_sha256,
            "state_root_sha256": self.state_root_sha256,
            "verified_at": _aware(self.verified_at, "verified_at").isoformat(),
        }


class QualificationEvidenceRegistry:
    """Append-only lifecycle authority with authenticated current-state root."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[EvidenceRegistryEvent] = []
        self._records: dict[str, EvidenceRegistryRecord] = {}
        self._signature_replay = QualificationSignatureReplayLedger()

    @property
    def head_sha256(self) -> str:
        with self._lock:
            return self._events[-1].event_sha256 if self._events else _GENESIS

    @property
    def state_root_sha256(self) -> str:
        with self._lock:
            return _state_root(self._records)

    @property
    def event_count(self) -> int:
        with self._lock:
            return len(self._events)

    @property
    def latest_observed_at(self) -> datetime:
        with self._lock:
            if not self._events:
                return datetime(1970, 1, 1, tzinfo=UTC)
            return self._events[-1].observed_at

    def register(
        self,
        *,
        manifest: QualificationManifest,
        binding: ProfileBoundQualificationManifest,
        signed_evidence: SignedQualificationEvidence,
        keyring: VerifiedQualificationKeyring,
        observed_at: datetime,
        max_clock_skew_seconds: int = 5,
    ) -> EvidenceRegistryRecord:
        manifest.validate()
        binding.validate()
        signed_evidence.validate()
        now = _aware(observed_at, "observed_at")
        if now < _aware(manifest.issued_at, "manifest.issued_at"):
            raise ValueError("qualification evidence cannot register before manifest issuance")
        if binding.manifest_id != manifest.manifest_id:
            raise ValueError("registry binding manifest id mismatch")
        if binding.manifest_sha256 != manifest.manifest_sha256:
            raise ValueError("registry binding manifest digest mismatch")
        if signed_evidence.binding_id != binding.binding_id:
            raise ValueError("registry signed binding id mismatch")
        if signed_evidence.binding_sha256 != binding.binding_sha256:
            raise ValueError("registry signed binding digest mismatch")
        if signed_evidence.profile_sha256 != binding.profile_sha256:
            raise ValueError("registry signed profile digest mismatch")

        evidence_id = signed_evidence.evidence_id
        with self._lock:
            if evidence_id in self._records:
                raise ValueError("qualification evidence is already registered")

        verification = verify_qualification_evidence(
            binding=binding,
            signed_evidence=signed_evidence,
            keyring=keyring,
            observed_at=now,
            replay_ledger=self._signature_replay,
            max_clock_skew_seconds=max_clock_skew_seconds,
        )
        if verification.manifest_sha256 != manifest.manifest_sha256:
            raise ValueError("registry verified manifest mismatch")
        if verification.profile_sha256 != binding.profile_sha256:
            raise ValueError("registry verified profile mismatch")

        with self._lock:
            if evidence_id in self._records:
                raise ValueError("qualification evidence is already registered")
            previous = self._events[-1].event_sha256 if self._events else _GENESIS
            event = EvidenceRegistryEvent(
                sequence=len(self._events) + 1,
                event_type=EvidenceRegistryEventType.REGISTERED,
                evidence_id=evidence_id,
                binding_id=binding.binding_id,
                binding_sha256=binding.binding_sha256,
                manifest_id=manifest.manifest_id,
                manifest_sha256=manifest.manifest_sha256,
                profile_sha256=binding.profile_sha256,
                subject=manifest.subject,
                subject_version=manifest.subject_version,
                profile_id=manifest.profile_id,
                profile_version=manifest.profile_version,
                signer_key_id=verification.signer_key_id,
                signer_owner_id=verification.signer_owner_id,
                signer_key_generation=verification.signer_key_generation,
                keyring_generation=verification.keyring_generation,
                signature_envelope_sha256=signed_evidence.envelope.envelope_sha256,
                cryptographically_verified_at=verification.verified_at,
                observed_at=now,
                previous_event_sha256=previous,
            )
            event.validate()
            record = EvidenceRegistryRecord(
                evidence_id=evidence_id,
                binding_id=binding.binding_id,
                binding_sha256=binding.binding_sha256,
                manifest_id=manifest.manifest_id,
                manifest_sha256=manifest.manifest_sha256,
                profile_sha256=binding.profile_sha256,
                subject=manifest.subject,
                subject_version=manifest.subject_version,
                profile_id=manifest.profile_id,
                profile_version=manifest.profile_version,
                signer_key_id=verification.signer_key_id,
                signer_owner_id=verification.signer_owner_id,
                signer_key_generation=verification.signer_key_generation,
                keyring_generation=verification.keyring_generation,
                signature_envelope_sha256=signed_evidence.envelope.envelope_sha256,
                cryptographically_verified_at=verification.verified_at,
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
        if not reason.strip():
            raise ValueError("supersession reason is required")
        now = _aware(observed_at, "observed_at")
        with self._lock:
            current = self._require_active(evidence_id)
            replacement = self._require_active(replacement_evidence_id)
            if replacement.evidence_id == current.evidence_id:
                raise ValueError("qualification evidence cannot supersede itself")
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
            updated = self._closed_record(
                current=current,
                event=event,
                status=EvidenceLifecycleStatus.SUPERSEDED,
                reason=reason,
                replacement_evidence_id=replacement_evidence_id,
            )
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
            updated = self._closed_record(
                current=current,
                event=event,
                status=EvidenceLifecycleStatus.REVOKED,
                reason=reason,
                replacement_evidence_id=None,
            )
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
            state_root = _state_root(self._records)
            if record is None:
                decision = EvidenceVerificationDecision(
                    evidence_id=evidence_id,
                    status=EvidenceVerificationStatus.UNKNOWN,
                    binding_id=None,
                    binding_sha256=None,
                    manifest_id=None,
                    manifest_sha256=None,
                    profile_sha256=None,
                    signer_key_id=None,
                    signer_owner_id=None,
                    signer_key_generation=None,
                    keyring_generation=None,
                    signature_envelope_sha256=None,
                    replacement_evidence_id=None,
                    reason="EVIDENCE_NOT_REGISTERED",
                    registry_head_sha256=head,
                    state_root_sha256=state_root,
                    verified_at=now,
                )
            else:
                status = {
                    EvidenceLifecycleStatus.ACTIVE: EvidenceVerificationStatus.VALID,
                    EvidenceLifecycleStatus.SUPERSEDED: EvidenceVerificationStatus.SUPERSEDED,
                    EvidenceLifecycleStatus.REVOKED: EvidenceVerificationStatus.REVOKED,
                }[record.status]
                decision = EvidenceVerificationDecision(
                    evidence_id=evidence_id,
                    status=status,
                    binding_id=record.binding_id,
                    binding_sha256=record.binding_sha256,
                    manifest_id=record.manifest_id,
                    manifest_sha256=record.manifest_sha256,
                    profile_sha256=record.profile_sha256,
                    signer_key_id=record.signer_key_id,
                    signer_owner_id=record.signer_owner_id,
                    signer_key_generation=record.signer_key_generation,
                    keyring_generation=record.keyring_generation,
                    signature_envelope_sha256=record.signature_envelope_sha256,
                    replacement_evidence_id=record.replacement_evidence_id,
                    reason=(
                        None
                        if record.status is EvidenceLifecycleStatus.ACTIVE
                        else record.lifecycle_reason
                    ),
                    registry_head_sha256=head,
                    state_root_sha256=state_root,
                    verified_at=now,
                )
            decision.validate()
            return decision

    def state_proof(self, *, evidence_id: str) -> EvidenceStateProof:
        with self._lock:
            ordered = tuple(sorted(self._records.values(), key=lambda item: item.evidence_id))
            index = next(
                (
                    position
                    for position, record in enumerate(ordered)
                    if record.evidence_id == evidence_id
                ),
                None,
            )
            if index is None:
                raise ValueError("qualification evidence is not registered")
            leaves = tuple(record.state_leaf_sha256 for record in ordered)
            proof = EvidenceStateProof(
                evidence_id=evidence_id,
                record=ordered[index],
                leaf_index=index,
                tree_size=len(leaves),
                audit_path=_merkle_path(leaves, index),
                state_root_sha256=_merkle_root(leaves),
            )
            proof.validate()
            return proof

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

    def events(self) -> tuple[EvidenceRegistryEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def _require_active(self, evidence_id: str) -> EvidenceRegistryRecord:
        record = self._records.get(evidence_id)
        if record is None:
            raise ValueError("qualification evidence is not registered")
        if record.status is not EvidenceLifecycleStatus.ACTIVE:
            raise ValueError("qualification evidence is not ACTIVE")
        return record

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
            binding_id=current.binding_id,
            binding_sha256=current.binding_sha256,
            manifest_id=current.manifest_id,
            manifest_sha256=current.manifest_sha256,
            profile_sha256=current.profile_sha256,
            subject=current.subject,
            subject_version=current.subject_version,
            profile_id=current.profile_id,
            profile_version=current.profile_version,
            signer_key_id=current.signer_key_id,
            signer_owner_id=current.signer_owner_id,
            signer_key_generation=current.signer_key_generation,
            keyring_generation=current.keyring_generation,
            signature_envelope_sha256=current.signature_envelope_sha256,
            cryptographically_verified_at=current.cryptographically_verified_at,
            observed_at=observed_at,
            previous_event_sha256=previous,
            reason=reason,
            replacement_evidence_id=replacement_evidence_id,
        )
        event.validate()
        return event

    def _closed_record(
        self,
        *,
        current: EvidenceRegistryRecord,
        event: EvidenceRegistryEvent,
        status: EvidenceLifecycleStatus,
        reason: str,
        replacement_evidence_id: str | None,
    ) -> EvidenceRegistryRecord:
        updated = EvidenceRegistryRecord(
            evidence_id=current.evidence_id,
            binding_id=current.binding_id,
            binding_sha256=current.binding_sha256,
            manifest_id=current.manifest_id,
            manifest_sha256=current.manifest_sha256,
            profile_sha256=current.profile_sha256,
            subject=current.subject,
            subject_version=current.subject_version,
            profile_id=current.profile_id,
            profile_version=current.profile_version,
            signer_key_id=current.signer_key_id,
            signer_owner_id=current.signer_owner_id,
            signer_key_generation=current.signer_key_generation,
            keyring_generation=current.keyring_generation,
            signature_envelope_sha256=current.signature_envelope_sha256,
            cryptographically_verified_at=current.cryptographically_verified_at,
            status=status,
            registered_at=current.registered_at,
            updated_at=event.observed_at,
            registration_event_sha256=current.registration_event_sha256,
            latest_event_sha256=event.event_sha256,
            replacement_evidence_id=replacement_evidence_id,
            lifecycle_reason=reason,
        )
        updated.validate()
        return updated


def verify_state_proof(proof: EvidenceStateProof) -> bool:
    proof.validate()
    computed = proof.record.state_leaf_sha256
    index = proof.leaf_index
    width = proof.tree_size
    path_index = 0
    while width > 1:
        sibling_exists = index % 2 == 1 or index + 1 < width
        if sibling_exists:
            if path_index >= len(proof.audit_path):
                return False
            sibling = proof.audit_path[path_index]
            path_index += 1
            if index % 2 == 1:
                computed = _node_hash(sibling, computed)
            else:
                computed = _node_hash(computed, sibling)
        index //= 2
        width = (width + 1) // 2
    return path_index == len(proof.audit_path) and computed == proof.state_root_sha256


def _state_root(records: dict[str, EvidenceRegistryRecord]) -> str:
    ordered = tuple(sorted(records.values(), key=lambda item: item.evidence_id))
    return _merkle_root(tuple(record.state_leaf_sha256 for record in ordered))


def _leaf_hash(payload_sha256: str) -> str:
    return hashlib.sha256(b"\x00" + bytes.fromhex(_digest(payload_sha256, "payload"))).hexdigest()


def _node_hash(left: str, right: str) -> str:
    return hashlib.sha256(
        b"\x01"
        + bytes.fromhex(_digest(left, "left"))
        + bytes.fromhex(_digest(right, "right"))
    ).hexdigest()


def _merkle_root(leaves: tuple[str, ...]) -> str:
    if not leaves:
        return _GENESIS
    level = tuple(_digest(item, "leaf") for item in leaves)
    while len(level) > 1:
        next_level: list[str] = []
        for index in range(0, len(level), 2):
            left = level[index]
            if index + 1 >= len(level):
                next_level.append(left)
            else:
                next_level.append(_node_hash(left, level[index + 1]))
        level = tuple(next_level)
    return level[0]


def _merkle_path(leaves: tuple[str, ...], leaf_index: int) -> tuple[str, ...]:
    if not leaves or leaf_index < 0 or leaf_index >= len(leaves):
        raise ValueError("invalid evidence state proof leaf")
    path: list[str] = []
    level = leaves
    index = leaf_index
    while len(level) > 1:
        if index % 2 == 1:
            path.append(level[index - 1])
        elif index + 1 < len(level):
            path.append(level[index + 1])
        next_level: list[str] = []
        for cursor in range(0, len(level), 2):
            left = level[cursor]
            if cursor + 1 >= len(level):
                next_level.append(left)
            else:
                next_level.append(_node_hash(left, level[cursor + 1]))
        index //= 2
        level = tuple(next_level)
    return tuple(path)


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
