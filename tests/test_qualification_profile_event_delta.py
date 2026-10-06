from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from app.qualification.profile_event_delta import (
    QualificationProfileEventDeltaProof,
    build_profile_event_delta,
    verify_profile_event_delta,
    verify_profile_event_delta_from_trusted_anchor,
)
from app.qualification.profile_registry import QualificationProfileRegistry
from tests.test_qualification_profile_registry import NOW, profile


def populated_registry() -> QualificationProfileRegistry:
    registry = QualificationProfileRegistry()
    first = profile(version="10.0.0")
    second = profile(version="10.1.0")
    registry.register(profile=first, observed_at=NOW)
    registry.register(profile=second, observed_at=NOW)
    registry.activate(
        profile_ref=first.profile_ref,
        observed_at=NOW + timedelta(seconds=1),
    )
    registry.activate(
        profile_ref=second.profile_ref,
        observed_at=NOW + timedelta(seconds=2),
    )
    registry.deprecate(
        profile_ref=first.profile_ref,
        superseded_by=second.profile_ref,
        reason="POLICY_HARDENED",
        observed_at=NOW + timedelta(seconds=3),
    )
    return registry


def test_delta_from_genesis_reproduces_current_profile_event_head() -> None:
    registry = populated_registry()

    proof = build_profile_event_delta(
        profile_registry=registry,
        previous_event_count=0,
    )
    verified = verify_profile_event_delta(proof)

    assert proof.previous_event_head_sha256 == "0" * 64
    assert proof.current_event_count == registry.event_count
    assert proof.current_event_head_sha256 == registry.event_head_sha256
    assert len(proof.appended_events) == registry.event_count
    assert verified.current_event_head_sha256 == registry.event_head_sha256
    assert verified.appended_event_count == registry.event_count


def test_incremental_delta_contains_only_missing_suffix() -> None:
    registry = populated_registry()
    events = registry.events()
    previous_count = 2

    proof = build_profile_event_delta(
        profile_registry=registry,
        previous_event_count=previous_count,
    )

    assert proof.previous_event_count == previous_count
    assert proof.previous_event_head_sha256 == events[previous_count - 1].event_sha256
    assert proof.appended_events == events[previous_count:]
    assert proof.appended_events[0].sequence == previous_count + 1
    assert proof.appended_events[0].previous_event_sha256 == (
        proof.previous_event_head_sha256
    )
    assert verify_profile_event_delta(proof).current_event_count == registry.event_count


def test_noop_delta_preserves_same_head() -> None:
    registry = populated_registry()

    proof = build_profile_event_delta(
        profile_registry=registry,
        previous_event_count=registry.event_count,
    )

    assert proof.appended_events == ()
    assert proof.previous_event_head_sha256 == registry.event_head_sha256
    assert proof.current_event_head_sha256 == registry.event_head_sha256
    assert verify_profile_event_delta(proof).appended_event_count == 0


def test_delta_rejects_skipped_event() -> None:
    registry = populated_registry()
    proof = build_profile_event_delta(
        profile_registry=registry,
        previous_event_count=0,
    )
    tampered = replace(
        proof,
        current_event_count=proof.current_event_count - 1,
        appended_events=proof.appended_events[1:],
    )

    with pytest.raises(
        ValueError,
        match="sequence mismatch|chain mismatch|appended event count mismatch",
    ):
        verify_profile_event_delta(tampered)


def test_delta_rejects_reordered_events() -> None:
    registry = populated_registry()
    proof = build_profile_event_delta(
        profile_registry=registry,
        previous_event_count=0,
    )
    reordered = (
        proof.appended_events[1],
        proof.appended_events[0],
        *proof.appended_events[2:],
    )
    tampered = replace(
        proof,
        appended_events=reordered,
    )

    with pytest.raises(ValueError, match="sequence mismatch|chain mismatch"):
        verify_profile_event_delta(tampered)


def test_delta_rejects_wrong_previous_head() -> None:
    registry = populated_registry()
    proof = build_profile_event_delta(
        profile_registry=registry,
        previous_event_count=2,
    )
    tampered = replace(
        proof,
        previous_event_head_sha256="f" * 64,
    )

    with pytest.raises(ValueError, match="chain mismatch"):
        verify_profile_event_delta(tampered)


def test_delta_rejects_wrong_current_head() -> None:
    registry = populated_registry()
    proof = build_profile_event_delta(
        profile_registry=registry,
        previous_event_count=2,
    )
    tampered = replace(
        proof,
        current_event_head_sha256="f" * 64,
    )

    with pytest.raises(ValueError, match="current head mismatch"):
        verify_profile_event_delta(tampered)


def test_delta_rejects_invalid_count_ranges() -> None:
    registry = populated_registry()

    with pytest.raises(ValueError, match="non-negative"):
        build_profile_event_delta(
            profile_registry=registry,
            previous_event_count=-1,
        )

    with pytest.raises(ValueError, match="exceeds current"):
        build_profile_event_delta(
            profile_registry=registry,
            previous_event_count=registry.event_count + 1,
        )

    with pytest.raises(ValueError, match="cannot precede"):
        QualificationProfileEventDeltaProof(
            previous_event_count=2,
            current_event_count=1,
            previous_event_head_sha256="a" * 64,
            current_event_head_sha256="b" * 64,
            appended_events=(),
        ).validate()



def test_delta_payload_is_deterministic() -> None:
    registry = populated_registry()
    proof = build_profile_event_delta(
        profile_registry=registry,
        previous_event_count=2,
    )

    assert proof.payload() == proof.payload()
    verified = verify_profile_event_delta(proof)
    assert verified.payload()["appended_event_count"] == len(proof.appended_events)


def test_delta_validation_rejects_invalid_genesis_and_noop_head_change() -> None:
    with pytest.raises(ValueError, match="requires genesis head"):
        QualificationProfileEventDeltaProof(
            previous_event_count=0,
            current_event_count=0,
            previous_event_head_sha256="f" * 64,
            current_event_head_sha256="f" * 64,
            appended_events=(),
        ).validate()

    with pytest.raises(ValueError, match="cannot change event head"):
        QualificationProfileEventDeltaProof(
            previous_event_count=2,
            current_event_count=2,
            previous_event_head_sha256="a" * 64,
            current_event_head_sha256="b" * 64,
            appended_events=(),
        ).validate()


def test_delta_builder_rejects_registry_mutation_during_snapshot() -> None:
    base = populated_registry()
    late = profile(version="10.2.0")

    class MutatingRegistry(QualificationProfileRegistry):
        def __init__(self) -> None:
            super().__init__()
            for event_profile in (
                profile(version="10.0.0"),
                profile(version="10.1.0"),
            ):
                self.register(profile=event_profile, observed_at=NOW)
            self.activate(
                profile_ref="ASTRA_BYBIT_PUBLIC_MARKETDATA@10.0.0",
                observed_at=NOW + timedelta(seconds=1),
            )
            self.activate(
                profile_ref="ASTRA_BYBIT_PUBLIC_MARKETDATA@10.1.0",
                observed_at=NOW + timedelta(seconds=2),
            )
            self.deprecate(
                profile_ref="ASTRA_BYBIT_PUBLIC_MARKETDATA@10.0.0",
                superseded_by="ASTRA_BYBIT_PUBLIC_MARKETDATA@10.1.0",
                reason="POLICY_HARDENED",
                observed_at=NOW + timedelta(seconds=3),
            )
            self._mutated = False

        def events(self):
            snapshot = super().events()
            if not self._mutated:
                self._mutated = True
                self.register(
                    profile=late,
                    observed_at=NOW + timedelta(seconds=4),
                )
            return snapshot

    registry = MutatingRegistry()
    assert registry.event_count == base.event_count

    with pytest.raises(ValueError, match="event count changed"):
        build_profile_event_delta(
            profile_registry=registry,
            previous_event_count=0,
        )



def test_delta_verifier_binds_exact_external_trusted_anchor() -> None:
    registry = populated_registry()
    proof = build_profile_event_delta(
        profile_registry=registry,
        previous_event_count=2,
    )

    verified = verify_profile_event_delta_from_trusted_anchor(
        proof,
        trusted_previous_event_count=2,
        trusted_previous_event_head_sha256=proof.previous_event_head_sha256,
    )
    assert verified.current_event_head_sha256 == registry.event_head_sha256

    with pytest.raises(ValueError, match="trusted count mismatch"):
        verify_profile_event_delta_from_trusted_anchor(
            proof,
            trusted_previous_event_count=1,
            trusted_previous_event_head_sha256=proof.previous_event_head_sha256,
        )

    with pytest.raises(ValueError, match="trusted head mismatch"):
        verify_profile_event_delta_from_trusted_anchor(
            proof,
            trusted_previous_event_count=2,
            trusted_previous_event_head_sha256="f" * 64,
        )

    with pytest.raises(ValueError, match="must be non-negative"):
        verify_profile_event_delta_from_trusted_anchor(
            proof,
            trusted_previous_event_count=-1,
            trusted_previous_event_head_sha256=proof.previous_event_head_sha256,
        )
