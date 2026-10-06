from __future__ import annotations

from dataclasses import dataclass

from app.qualification.profile_registry import (
    QualificationProfileEvent,
    QualificationProfileRegistry,
)

_SCHEMA_VERSION = "astra-qualification-profile-event-delta-v1"
_GENESIS = "0" * 64


@dataclass(frozen=True)
class QualificationProfileEventDeltaProof:
    previous_event_count: int
    current_event_count: int
    previous_event_head_sha256: str
    current_event_head_sha256: str
    appended_events: tuple[QualificationProfileEvent, ...]
    schema_version: str = _SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError("qualification profile event delta schema mismatch")
        if self.previous_event_count < 0:
            raise ValueError("previous_event_count must be non-negative")
        if self.current_event_count < self.previous_event_count:
            raise ValueError("current_event_count cannot precede previous_event_count")
        _digest(self.previous_event_head_sha256, "previous_event_head_sha256")
        _digest(self.current_event_head_sha256, "current_event_head_sha256")
        expected = self.current_event_count - self.previous_event_count
        if len(self.appended_events) != expected:
            raise ValueError("profile event delta appended event count mismatch")
        if self.previous_event_count == 0 and self.previous_event_head_sha256 != _GENESIS:
            raise ValueError("zero previous event count requires genesis head")
        if not self.appended_events:
            if self.current_event_head_sha256 != self.previous_event_head_sha256:
                raise ValueError("empty profile event delta cannot change event head")
            return

        expected_sequence = self.previous_event_count + 1
        previous = self.previous_event_head_sha256
        for event in self.appended_events:
            event.validate()
            if event.sequence != expected_sequence:
                raise ValueError("profile event delta sequence mismatch")
            if event.previous_event_sha256 != previous:
                raise ValueError("profile event delta chain mismatch")
            previous = event.event_sha256
            expected_sequence += 1
        if previous != self.current_event_head_sha256:
            raise ValueError("profile event delta current head mismatch")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "previous_event_count": self.previous_event_count,
            "current_event_count": self.current_event_count,
            "previous_event_head_sha256": self.previous_event_head_sha256,
            "current_event_head_sha256": self.current_event_head_sha256,
            "appended_events": [event.payload() for event in self.appended_events],
        }


@dataclass(frozen=True)
class VerifiedQualificationProfileEventDelta:
    previous_event_count: int
    current_event_count: int
    previous_event_head_sha256: str
    current_event_head_sha256: str
    appended_event_count: int

    def payload(self) -> dict[str, object]:
        return {
            "previous_event_count": self.previous_event_count,
            "current_event_count": self.current_event_count,
            "previous_event_head_sha256": self.previous_event_head_sha256,
            "current_event_head_sha256": self.current_event_head_sha256,
            "appended_event_count": self.appended_event_count,
        }


def build_profile_event_delta(
    *,
    profile_registry: QualificationProfileRegistry,
    previous_event_count: int,
) -> QualificationProfileEventDeltaProof:
    if previous_event_count < 0:
        raise ValueError("previous_event_count must be non-negative")
    events = profile_registry.events()
    current_count = len(events)
    if previous_event_count > current_count:
        raise ValueError("previous_event_count exceeds current profile event count")
    verified_head = profile_registry.verify_event_chain()
    if profile_registry.event_count != current_count:
        raise ValueError("profile event count changed while building delta")
    if profile_registry.event_head_sha256 != verified_head:
        raise ValueError("profile event head changed while building delta")

    previous_head = (
        _GENESIS
        if previous_event_count == 0
        else events[previous_event_count - 1].event_sha256
    )
    proof = QualificationProfileEventDeltaProof(
        previous_event_count=previous_event_count,
        current_event_count=current_count,
        previous_event_head_sha256=previous_head,
        current_event_head_sha256=verified_head,
        appended_events=events[previous_event_count:],
    )
    proof.validate()
    return proof


def verify_profile_event_delta(
    proof: QualificationProfileEventDeltaProof,
) -> VerifiedQualificationProfileEventDelta:
    proof.validate()
    return VerifiedQualificationProfileEventDelta(
        previous_event_count=proof.previous_event_count,
        current_event_count=proof.current_event_count,
        previous_event_head_sha256=proof.previous_event_head_sha256,
        current_event_head_sha256=proof.current_event_head_sha256,
        appended_event_count=len(proof.appended_events),
    )


def verify_profile_event_delta_from_trusted_anchor(
    proof: QualificationProfileEventDeltaProof,
    *,
    trusted_previous_event_count: int,
    trusted_previous_event_head_sha256: str,
) -> VerifiedQualificationProfileEventDelta:
    if trusted_previous_event_count < 0:
        raise ValueError("trusted_previous_event_count must be non-negative")
    trusted_head = _digest(
        trusted_previous_event_head_sha256,
        "trusted_previous_event_head_sha256",
    )
    if proof.previous_event_count != trusted_previous_event_count:
        raise ValueError("profile event delta trusted count mismatch")
    if proof.previous_event_head_sha256 != trusted_head:
        raise ValueError("profile event delta trusted head mismatch")
    return verify_profile_event_delta(proof)


def _digest(value: str, name: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{name} must be a sha256 digest")
    return normalized
