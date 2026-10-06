from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from app.qualification.evidence_registry import (
    EvidenceRegistryEvent,
    QualificationEvidenceRegistry,
)
from app.qualification.profile_registry import (
    QualificationProfileEvent,
    QualificationProfileRegistry,
)
from app.qualification.transparency_log import (
    QualificationInclusionProof,
    QualificationTransparencyEntry,
    QualificationTransparencyLog,
    QualificationTransparencyTreeHead,
    verify_inclusion_proof,
)

_SCHEMA_VERSION = "astra-qualification-lifecycle-transparency-coverage-v1"
_EVIDENCE_ENTRY_TYPE = "QUALIFICATION_EVIDENCE_REGISTRY_EVENT"
_PROFILE_ENTRY_TYPE = "QUALIFICATION_PROFILE_REGISTRY_EVENT"
_GENESIS = "0" * 64


@dataclass(frozen=True)
class QualificationLifecyclePublicationProof:
    event_domain: str
    sequence: int
    event_sha256: str
    observed_at: datetime
    entry: QualificationTransparencyEntry
    inclusion_proof: QualificationInclusionProof

    def validate(self) -> None:
        if self.event_domain not in {"EVIDENCE", "PROFILE"}:
            raise ValueError("qualification lifecycle publication domain mismatch")
        if self.sequence < 1:
            raise ValueError("qualification lifecycle publication sequence must be positive")
        _digest(self.event_sha256, "event_sha256")
        observed = _aware(self.observed_at, "observed_at")
        self.entry.validate()
        self.inclusion_proof.validate()
        expected_id = lifecycle_event_object_id(
            event_domain=self.event_domain,
            sequence=self.sequence,
            event_sha256=self.event_sha256,
        )
        if self.entry.object_id != expected_id:
            raise ValueError("qualification lifecycle publication object id mismatch")
        if self.entry.object_sha256 != self.event_sha256:
            raise ValueError("qualification lifecycle publication event digest mismatch")
        expected_type = (
            _EVIDENCE_ENTRY_TYPE
            if self.event_domain == "EVIDENCE"
            else _PROFILE_ENTRY_TYPE
        )
        if self.entry.entry_type != expected_type:
            raise ValueError("qualification lifecycle publication entry type mismatch")
        if _aware(self.entry.published_at, "entry.published_at") < observed:
            raise ValueError("qualification lifecycle publication predates event")
        if self.inclusion_proof.leaf_sha256 != self.entry.leaf_sha256:
            raise ValueError("qualification lifecycle publication leaf mismatch")


@dataclass(frozen=True)
class QualificationLifecycleTransparencyCoverage:
    evidence_events: tuple[EvidenceRegistryEvent, ...]
    evidence_publications: tuple[QualificationLifecyclePublicationProof, ...]
    profile_events: tuple[QualificationProfileEvent, ...]
    profile_publications: tuple[QualificationLifecyclePublicationProof, ...]
    transparency_head: QualificationTransparencyTreeHead
    audited_at: datetime
    schema_version: str = _SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError("qualification lifecycle transparency schema mismatch")
        self.transparency_head.validate()
        audited = _aware(self.audited_at, "audited_at")
        if audited < _aware(self.transparency_head.issued_at, "transparency_head.issued_at"):
            raise ValueError("qualification lifecycle audit predates transparency head")
        if len(self.evidence_events) != len(self.evidence_publications):
            raise ValueError("evidence lifecycle publication count mismatch")
        if len(self.profile_events) != len(self.profile_publications):
            raise ValueError("profile lifecycle publication count mismatch")

        _validate_evidence_chain(self.evidence_events)
        _validate_profile_chain(self.profile_events)

        for event, publication in zip(
            self.evidence_events,
            self.evidence_publications,
            strict=True,
        ):
            publication.validate()
            if publication.event_domain != "EVIDENCE":
                raise ValueError("evidence lifecycle publication domain mismatch")
            if publication.sequence != event.sequence:
                raise ValueError("evidence lifecycle publication sequence mismatch")
            if publication.event_sha256 != event.event_sha256:
                raise ValueError("evidence lifecycle publication digest mismatch")
            if _aware(publication.observed_at, "publication.observed_at") != _aware(
                event.observed_at,
                "event.observed_at",
            ):
                raise ValueError("evidence lifecycle publication time mismatch")
            _validate_proof_head(publication.inclusion_proof, self.transparency_head)

        for event, publication in zip(
            self.profile_events,
            self.profile_publications,
            strict=True,
        ):
            publication.validate()
            if publication.event_domain != "PROFILE":
                raise ValueError("profile lifecycle publication domain mismatch")
            if publication.sequence != event.sequence:
                raise ValueError("profile lifecycle publication sequence mismatch")
            if publication.event_sha256 != event.event_sha256:
                raise ValueError("profile lifecycle publication digest mismatch")
            if _aware(publication.observed_at, "publication.observed_at") != _aware(
                event.observed_at,
                "event.observed_at",
            ):
                raise ValueError("profile lifecycle publication time mismatch")
            _validate_proof_head(publication.inclusion_proof, self.transparency_head)

    @property
    def evidence_event_count(self) -> int:
        return len(self.evidence_events)

    @property
    def evidence_event_head_sha256(self) -> str:
        return self.evidence_events[-1].event_sha256 if self.evidence_events else _GENESIS

    @property
    def profile_event_count(self) -> int:
        return len(self.profile_events)

    @property
    def profile_event_head_sha256(self) -> str:
        return self.profile_events[-1].event_sha256 if self.profile_events else _GENESIS

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "evidence_event_count": self.evidence_event_count,
            "evidence_event_head_sha256": self.evidence_event_head_sha256,
            "evidence_events": [event.payload() for event in self.evidence_events],
            "evidence_publications": [
                _publication_payload(publication)
                for publication in self.evidence_publications
            ],
            "profile_event_count": self.profile_event_count,
            "profile_event_head_sha256": self.profile_event_head_sha256,
            "profile_events": [event.payload() for event in self.profile_events],
            "profile_publications": [
                _publication_payload(publication)
                for publication in self.profile_publications
            ],
            "transparency_head": self.transparency_head.payload(),
            "audited_at": _aware(self.audited_at, "audited_at").isoformat(),
        }

    @property
    def coverage_sha256(self) -> str:
        return _sha256(self.payload())

    @property
    def coverage_id(self) -> str:
        return f"qlifecycle_{self.coverage_sha256[:24]}"


def lifecycle_event_object_id(
    *,
    event_domain: str,
    sequence: int,
    event_sha256: str,
) -> str:
    if event_domain not in {"EVIDENCE", "PROFILE"}:
        raise ValueError("qualification lifecycle event domain mismatch")
    if sequence < 1:
        raise ValueError("qualification lifecycle event sequence must be positive")
    digest = _digest(event_sha256, "event_sha256")
    prefix = "qevidenceevent" if event_domain == "EVIDENCE" else "qprofileevent"
    return f"{prefix}_{sequence}_{digest}"


def publish_evidence_lifecycle_event(
    *,
    event: EvidenceRegistryEvent,
    transparency_log: QualificationTransparencyLog,
    published_at: datetime,
) -> QualificationTransparencyTreeHead:
    event.validate()
    published = _aware(published_at, "published_at")
    if published < _aware(event.observed_at, "event.observed_at"):
        raise ValueError("evidence lifecycle publication cannot predate event")
    entry = QualificationTransparencyEntry(
        entry_type=_EVIDENCE_ENTRY_TYPE,
        object_id=lifecycle_event_object_id(
            event_domain="EVIDENCE",
            sequence=event.sequence,
            event_sha256=event.event_sha256,
        ),
        object_sha256=event.event_sha256,
        subject=event.subject,
        subject_version=event.subject_version,
        profile_id=event.profile_id,
        profile_version=event.profile_version,
        published_at=published,
    )
    return transparency_log.append(entry=entry, issued_at=published)


def publish_profile_lifecycle_event(
    *,
    event: QualificationProfileEvent,
    transparency_log: QualificationTransparencyLog,
    published_at: datetime,
) -> QualificationTransparencyTreeHead:
    event.validate()
    published = _aware(published_at, "published_at")
    if published < _aware(event.observed_at, "event.observed_at"):
        raise ValueError("profile lifecycle publication cannot predate event")
    profile_id, profile_version = _profile_ref_parts(event.profile_ref)
    entry = QualificationTransparencyEntry(
        entry_type=_PROFILE_ENTRY_TYPE,
        object_id=lifecycle_event_object_id(
            event_domain="PROFILE",
            sequence=event.sequence,
            event_sha256=event.event_sha256,
        ),
        object_sha256=event.event_sha256,
        subject="QUALIFICATION_PROFILE_POLICY",
        subject_version=event.event_type.value,
        profile_id=profile_id,
        profile_version=profile_version,
        published_at=published,
    )
    return transparency_log.append(entry=entry, issued_at=published)


def build_lifecycle_transparency_coverage(
    *,
    evidence_registry: QualificationEvidenceRegistry,
    profile_registry: QualificationProfileRegistry,
    transparency_log: QualificationTransparencyLog,
    audited_at: datetime,
) -> QualificationLifecycleTransparencyCoverage:
    now = _aware(audited_at, "audited_at")
    evidence_events = evidence_registry.events()
    profile_events = profile_registry.events()
    evidence_registry.verify_chain()
    profile_registry.verify_event_chain()
    head = transparency_log.latest_head()
    if now < _aware(head.issued_at, "transparency_head.issued_at"):
        raise ValueError("qualification lifecycle audit predates transparency head")

    entries = {entry.object_id: entry for entry in transparency_log.entries()}
    evidence_publications = tuple(
        _publication_for_evidence_event(
            event=event,
            entries=entries,
            transparency_log=transparency_log,
        )
        for event in evidence_events
    )
    profile_publications = tuple(
        _publication_for_profile_event(
            event=event,
            entries=entries,
            transparency_log=transparency_log,
        )
        for event in profile_events
    )
    coverage = QualificationLifecycleTransparencyCoverage(
        evidence_events=evidence_events,
        evidence_publications=evidence_publications,
        profile_events=profile_events,
        profile_publications=profile_publications,
        transparency_head=head,
        audited_at=now,
    )
    coverage.validate()
    if not verify_lifecycle_transparency_coverage(coverage):
        raise ValueError("qualification lifecycle transparency coverage is invalid")
    return coverage


def verify_lifecycle_transparency_coverage(
    coverage: QualificationLifecycleTransparencyCoverage,
) -> bool:
    try:
        coverage.validate()
    except ValueError:
        return False
    return all(
        verify_inclusion_proof(publication.inclusion_proof)
        for publication in (
            *coverage.evidence_publications,
            *coverage.profile_publications,
        )
    )


def _publication_for_evidence_event(
    *,
    event: EvidenceRegistryEvent,
    entries: dict[str, QualificationTransparencyEntry],
    transparency_log: QualificationTransparencyLog,
) -> QualificationLifecyclePublicationProof:
    object_id = lifecycle_event_object_id(
        event_domain="EVIDENCE",
        sequence=event.sequence,
        event_sha256=event.event_sha256,
    )
    entry = entries.get(object_id)
    if entry is None:
        raise ValueError(
            f"evidence lifecycle event is not published: sequence={event.sequence}"
        )
    publication = QualificationLifecyclePublicationProof(
        event_domain="EVIDENCE",
        sequence=event.sequence,
        event_sha256=event.event_sha256,
        observed_at=event.observed_at,
        entry=entry,
        inclusion_proof=transparency_log.inclusion_proof(object_id=object_id),
    )
    publication.validate()
    return publication


def _publication_for_profile_event(
    *,
    event: QualificationProfileEvent,
    entries: dict[str, QualificationTransparencyEntry],
    transparency_log: QualificationTransparencyLog,
) -> QualificationLifecyclePublicationProof:
    object_id = lifecycle_event_object_id(
        event_domain="PROFILE",
        sequence=event.sequence,
        event_sha256=event.event_sha256,
    )
    entry = entries.get(object_id)
    if entry is None:
        raise ValueError(
            f"profile lifecycle event is not published: sequence={event.sequence}"
        )
    publication = QualificationLifecyclePublicationProof(
        event_domain="PROFILE",
        sequence=event.sequence,
        event_sha256=event.event_sha256,
        observed_at=event.observed_at,
        entry=entry,
        inclusion_proof=transparency_log.inclusion_proof(object_id=object_id),
    )
    publication.validate()
    return publication


def _validate_evidence_chain(events: tuple[EvidenceRegistryEvent, ...]) -> None:
    previous = _GENESIS
    for expected_sequence, event in enumerate(events, start=1):
        event.validate()
        if event.sequence != expected_sequence:
            raise ValueError("evidence lifecycle coverage sequence mismatch")
        if event.previous_event_sha256 != previous:
            raise ValueError("evidence lifecycle coverage chain mismatch")
        previous = event.event_sha256


def _validate_profile_chain(events: tuple[QualificationProfileEvent, ...]) -> None:
    previous = _GENESIS
    for expected_sequence, event in enumerate(events, start=1):
        event.validate()
        if event.sequence != expected_sequence:
            raise ValueError("profile lifecycle coverage sequence mismatch")
        if event.previous_event_sha256 != previous:
            raise ValueError("profile lifecycle coverage chain mismatch")
        previous = event.event_sha256


def _validate_proof_head(
    proof: QualificationInclusionProof,
    head: QualificationTransparencyTreeHead,
) -> None:
    if proof.tree_size != head.tree_size:
        raise ValueError("lifecycle publication proof tree size mismatch")
    if proof.root_sha256 != head.root_sha256:
        raise ValueError("lifecycle publication proof root mismatch")


def _publication_payload(
    publication: QualificationLifecyclePublicationProof,
) -> dict[str, object]:
    publication.validate()
    return {
        "event_domain": publication.event_domain,
        "sequence": publication.sequence,
        "event_sha256": publication.event_sha256,
        "observed_at": _aware(
            publication.observed_at,
            "publication.observed_at",
        ).isoformat(),
        "entry": publication.entry.payload(),
        "inclusion_proof": {
            "tree_size": publication.inclusion_proof.tree_size,
            "leaf_index": publication.inclusion_proof.leaf_index,
            "leaf_sha256": publication.inclusion_proof.leaf_sha256,
            "audit_path": list(publication.inclusion_proof.audit_path),
            "root_sha256": publication.inclusion_proof.root_sha256,
        },
    }


def _profile_ref_parts(profile_ref: str) -> tuple[str, str]:
    if "@" not in profile_ref:
        raise ValueError("qualification profile_ref must contain version")
    profile_id, version = profile_ref.rsplit("@", 1)
    if not profile_id.strip() or not version.strip():
        raise ValueError("qualification profile_ref is invalid")
    return profile_id, version


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
