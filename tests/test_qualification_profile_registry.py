from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.qualification.profile_registry import (
    QualificationProfile,
    QualificationProfileEventType,
    QualificationProfileRegistry,
    QualificationProfileStatus,
    verify_profile_state_proof,
)

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)


def profile(
    *,
    version: str,
    profile_id: str = "ASTRA_BYBIT_PUBLIC_MARKETDATA",
) -> QualificationProfile:
    value = QualificationProfile(
        profile_id=profile_id,
        version=version,
        scope="PUBLIC_MARKET_DATA_ONLY",
        allowed_environments=("mainnet-public-readonly",),
        required_corpus_classes=("BYBIT_PUBLIC_KLINE",),
        required_checks=(
            "BYBIT-PMD-001-SUBSCRIPTION-IDENTITY",
            "BYBIT-PMD-002-INSTRUMENT-SPEC",
            "BYBIT-PMD-003-RAW-CAPTURE-INTEGRITY",
        ),
        required_assertions=(
            "PROVIDER_COMPLETE_REPLAY_BOUND",
            "ADAPTER_CONFORMANCE_PASS",
            "MARKET_DATA_INTEGRITY_HEALTHY",
            "READ_ONLY_PUBLIC_MARKETDATA_SCOPE",
        ),
        required_limitations=(
            "EXECUTION_ORDER_SUBMIT_NOT_QUALIFIED",
            "PROFITABILITY_NOT_PROVEN",
        ),
        created_at=NOW,
    )
    value.validate()
    return value


def test_profile_definition_is_deterministic_and_hash_addressed() -> None:
    first = profile(version="1.0.0")
    second = profile(version="1.0.0")

    assert first == second
    assert first.profile_sha256 == second.profile_sha256
    assert first.profile_ref == "ASTRA_BYBIT_PUBLIC_MARKETDATA@1.0.0"
    assert len(first.profile_sha256) == 64


def test_profile_version_is_immutable_once_registered() -> None:
    registry = QualificationProfileRegistry()
    original = profile(version="1.0.0")
    registry.register(profile=original, observed_at=NOW)

    with pytest.raises(ValueError, match="immutable"):
        registry.register(
            profile=replace(
                original,
                required_checks=("DIFFERENT-CHECK",),
            ),
            observed_at=NOW + timedelta(seconds=1),
        )


def test_active_profile_admits_only_allowed_environment_and_required_corpus() -> None:
    registry = QualificationProfileRegistry()
    value = profile(version="1.0.0")
    registry.register(profile=value, observed_at=NOW)
    active = registry.activate(
        profile_ref=value.profile_ref,
        observed_at=NOW + timedelta(seconds=1),
    )
    assert active.status is QualificationProfileStatus.ACTIVE

    admitted = registry.require_active(
        profile_id=value.profile_id,
        version=value.version,
        environment="mainnet-public-readonly",
        corpus_classes=("BYBIT_PUBLIC_KLINE", "EXTRA_CLASS"),
        observed_at=NOW + timedelta(seconds=2),
    )
    assert admitted.profile_sha256 == value.profile_sha256

    with pytest.raises(ValueError, match="environment"):
        registry.require_active(
            profile_id=value.profile_id,
            version=value.version,
            environment="testnet",
            corpus_classes=("BYBIT_PUBLIC_KLINE",),
            observed_at=NOW + timedelta(seconds=2),
        )

    with pytest.raises(ValueError, match="corpus classes missing"):
        registry.require_active(
            profile_id=value.profile_id,
            version=value.version,
            environment="mainnet-public-readonly",
            corpus_classes=("OTHER",),
            observed_at=NOW + timedelta(seconds=2),
        )


def test_profile_deprecation_requires_active_same_family_replacement() -> None:
    registry = QualificationProfileRegistry()
    old = profile(version="1.0.0")
    new = profile(version="1.1.0")
    registry.register(profile=old, observed_at=NOW)
    registry.register(profile=new, observed_at=NOW)
    registry.activate(profile_ref=old.profile_ref, observed_at=NOW + timedelta(seconds=1))
    registry.activate(profile_ref=new.profile_ref, observed_at=NOW + timedelta(seconds=2))

    deprecated = registry.deprecate(
        profile_ref=old.profile_ref,
        superseded_by=new.profile_ref,
        reason="POLICY_HARDENED",
        observed_at=NOW + timedelta(seconds=3),
    )

    assert deprecated.status is QualificationProfileStatus.DEPRECATED
    assert deprecated.superseded_by == new.profile_ref
    assert deprecated.lifecycle_reason == "POLICY_HARDENED"

    with pytest.raises(ValueError, match="not ACTIVE"):
        registry.require_active(
            profile_id=old.profile_id,
            version=old.version,
            environment="mainnet-public-readonly",
            corpus_classes=("BYBIT_PUBLIC_KLINE",),
            observed_at=NOW + timedelta(seconds=4),
        )


def test_profile_deprecation_rejects_cross_family_replacement() -> None:
    registry = QualificationProfileRegistry()
    old = profile(version="1.0.0")
    other = profile(version="1.0.0", profile_id="ASTRA_OTHER_PROFILE")
    registry.register(profile=old, observed_at=NOW)
    registry.register(profile=other, observed_at=NOW)
    registry.activate(profile_ref=old.profile_ref, observed_at=NOW + timedelta(seconds=1))
    registry.activate(profile_ref=other.profile_ref, observed_at=NOW + timedelta(seconds=2))

    with pytest.raises(ValueError, match="family mismatch"):
        registry.deprecate(
            profile_ref=old.profile_ref,
            superseded_by=other.profile_ref,
            reason="INVALID_REPLACEMENT",
            observed_at=NOW + timedelta(seconds=3),
        )


def test_profile_revocation_is_terminal() -> None:
    registry = QualificationProfileRegistry()
    value = profile(version="2.0.0")
    registry.register(profile=value, observed_at=NOW)
    registry.activate(profile_ref=value.profile_ref, observed_at=NOW + timedelta(seconds=1))

    revoked = registry.revoke(
        profile_ref=value.profile_ref,
        reason="PROFILE_SECURITY_WITHDRAWAL",
        observed_at=NOW + timedelta(seconds=2),
    )
    assert revoked.status is QualificationProfileStatus.REVOKED

    with pytest.raises(ValueError, match="already closed"):
        registry.revoke(
            profile_ref=value.profile_ref,
            reason="SECOND_REVOKE",
            observed_at=NOW + timedelta(seconds=3),
        )


def test_profile_registry_rejects_time_regression() -> None:
    registry = QualificationProfileRegistry()
    value = profile(version="3.0.0")
    registry.register(profile=value, observed_at=NOW + timedelta(seconds=2))

    with pytest.raises(ValueError, match="time regression"):
        registry.activate(
            profile_ref=value.profile_ref,
            observed_at=NOW + timedelta(seconds=1),
        )



def test_profile_registry_rejects_duplicate_registration() -> None:
    registry = QualificationProfileRegistry()
    value = profile(version="4.0.0")
    registry.register(profile=value, observed_at=NOW)

    with pytest.raises(ValueError, match="already registered"):
        registry.register(
            profile=value,
            observed_at=NOW + timedelta(seconds=1),
        )


def test_profile_cannot_be_registered_before_creation() -> None:
    registry = QualificationProfileRegistry()
    value = replace(
        profile(version="4.1.0"),
        created_at=NOW + timedelta(seconds=10),
    )

    with pytest.raises(ValueError, match="before creation"):
        registry.register(
            profile=value,
            observed_at=NOW,
        )


def test_profile_cannot_be_activated_twice() -> None:
    registry = QualificationProfileRegistry()
    value = profile(version="4.2.0")
    registry.register(profile=value, observed_at=NOW)
    registry.activate(
        profile_ref=value.profile_ref,
        observed_at=NOW + timedelta(seconds=1),
    )

    with pytest.raises(ValueError, match="only DRAFT"):
        registry.activate(
            profile_ref=value.profile_ref,
            observed_at=NOW + timedelta(seconds=2),
        )


def test_deprecation_requires_active_replacement() -> None:
    registry = QualificationProfileRegistry()
    old = profile(version="4.3.0")
    replacement = profile(version="4.4.0")
    registry.register(profile=old, observed_at=NOW)
    registry.register(profile=replacement, observed_at=NOW)
    registry.activate(
        profile_ref=old.profile_ref,
        observed_at=NOW + timedelta(seconds=1),
    )

    with pytest.raises(ValueError, match="replacement qualification profile must be ACTIVE"):
        registry.deprecate(
            profile_ref=old.profile_ref,
            superseded_by=replacement.profile_ref,
            reason="NOT_READY",
            observed_at=NOW + timedelta(seconds=2),
        )


def test_profile_registry_get_and_active_versions() -> None:
    registry = QualificationProfileRegistry()
    first = profile(version="5.0.0")
    second = profile(version="5.1.0")
    registry.register(profile=first, observed_at=NOW)
    registry.register(profile=second, observed_at=NOW)
    registry.activate(
        profile_ref=second.profile_ref,
        observed_at=NOW + timedelta(seconds=1),
    )

    assert registry.get(first.profile_ref) is not None
    assert registry.get("missing@0") is None
    active = registry.active_versions(first.profile_id)
    assert active == (second,)


def test_profile_definition_rejects_duplicate_and_blank_policy_entries() -> None:
    duplicate = replace(
        profile(version="6.0.0"),
        required_checks=("CHECK", "CHECK"),
    )
    with pytest.raises(ValueError, match="required_checks must be unique"):
        duplicate.validate()

    blank = replace(
        profile(version="6.1.0"),
        required_limitations=("PROFITABILITY_NOT_PROVEN", ""),
    )
    with pytest.raises(ValueError, match="cannot contain blank"):
        blank.validate()



def test_profile_state_root_authenticates_current_active_state() -> None:
    registry = QualificationProfileRegistry()
    first = profile(version="7.0.0")
    second = profile(version="7.1.0")
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

    proof = registry.state_proof(profile_ref=first.profile_ref)

    assert proof.record.status is QualificationProfileStatus.ACTIVE
    assert proof.record.profile.profile_sha256 == first.profile_sha256
    assert proof.state_root_sha256 == registry.state_root_sha256
    assert verify_profile_state_proof(proof)


def test_profile_state_root_changes_when_policy_is_revoked() -> None:
    registry = QualificationProfileRegistry()
    value = profile(version="7.2.0")
    registry.register(profile=value, observed_at=NOW)
    registry.activate(
        profile_ref=value.profile_ref,
        observed_at=NOW + timedelta(seconds=1),
    )
    active_root = registry.state_root_sha256
    active_proof = registry.state_proof(profile_ref=value.profile_ref)
    assert verify_profile_state_proof(active_proof)

    registry.revoke(
        profile_ref=value.profile_ref,
        reason="PROFILE_SECURITY_WITHDRAWAL",
        observed_at=NOW + timedelta(seconds=2),
    )

    revoked_root = registry.state_root_sha256
    revoked_proof = registry.state_proof(profile_ref=value.profile_ref)
    assert revoked_root != active_root
    assert revoked_proof.record.status is QualificationProfileStatus.REVOKED
    assert verify_profile_state_proof(revoked_proof)
    assert not verify_profile_state_proof(
        replace(active_proof, state_root_sha256=revoked_root)
    )


def test_profile_state_root_changes_when_policy_is_deprecated() -> None:
    registry = QualificationProfileRegistry()
    old = profile(version="7.3.0")
    replacement = profile(version="7.4.0")
    registry.register(profile=old, observed_at=NOW)
    registry.register(profile=replacement, observed_at=NOW)
    registry.activate(
        profile_ref=old.profile_ref,
        observed_at=NOW + timedelta(seconds=1),
    )
    registry.activate(
        profile_ref=replacement.profile_ref,
        observed_at=NOW + timedelta(seconds=2),
    )
    active_root = registry.state_root_sha256

    registry.deprecate(
        profile_ref=old.profile_ref,
        superseded_by=replacement.profile_ref,
        reason="POLICY_HARDENED",
        observed_at=NOW + timedelta(seconds=3),
    )

    proof = registry.state_proof(profile_ref=old.profile_ref)
    assert registry.state_root_sha256 != active_root
    assert proof.record.status is QualificationProfileStatus.DEPRECATED
    assert proof.record.superseded_by == replacement.profile_ref
    assert verify_profile_state_proof(proof)


def test_profile_state_proof_rejects_unknown_and_tampered_path() -> None:
    registry = QualificationProfileRegistry()
    first = profile(version="7.5.0")
    second = profile(version="7.6.0")
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

    with pytest.raises(ValueError, match="not registered"):
        registry.state_proof(profile_ref="missing@0")

    proof = registry.state_proof(profile_ref=first.profile_ref)
    assert proof.audit_path
    assert not verify_profile_state_proof(
        replace(
            proof,
            audit_path=("f" * 64, *proof.audit_path[1:]),
        )
    )


def test_empty_profile_registry_has_genesis_state_root() -> None:
    registry = QualificationProfileRegistry()

    assert registry.state_root_sha256 == "0" * 64



def test_profile_lifecycle_events_are_append_only_and_hash_chained() -> None:
    registry = QualificationProfileRegistry()
    value = profile(version="8.0.0")
    registry.register(profile=value, observed_at=NOW)
    registry.activate(
        profile_ref=value.profile_ref,
        observed_at=NOW + timedelta(seconds=1),
    )
    registry.revoke(
        profile_ref=value.profile_ref,
        reason="POLICY_SECURITY_WITHDRAWAL",
        observed_at=NOW + timedelta(seconds=2),
    )

    events = registry.events()

    assert registry.event_count == 3
    assert tuple(event.sequence for event in events) == (1, 2, 3)
    assert tuple(event.event_type for event in events) == (
        QualificationProfileEventType.REGISTERED,
        QualificationProfileEventType.ACTIVATED,
        QualificationProfileEventType.REVOKED,
    )
    assert events[0].previous_event_sha256 == "0" * 64
    assert events[1].previous_event_sha256 == events[0].event_sha256
    assert events[2].previous_event_sha256 == events[1].event_sha256
    assert events[2].reason == "POLICY_SECURITY_WITHDRAWAL"
    assert registry.verify_event_chain() == registry.event_head_sha256
    assert registry.event_head_sha256 == events[-1].event_sha256


def test_profile_deprecation_event_authenticates_reason_and_replacement() -> None:
    registry = QualificationProfileRegistry()
    old = profile(version="8.1.0")
    replacement = profile(version="8.2.0")
    registry.register(profile=old, observed_at=NOW)
    registry.register(profile=replacement, observed_at=NOW)
    registry.activate(
        profile_ref=old.profile_ref,
        observed_at=NOW + timedelta(seconds=1),
    )
    registry.activate(
        profile_ref=replacement.profile_ref,
        observed_at=NOW + timedelta(seconds=2),
    )
    registry.deprecate(
        profile_ref=old.profile_ref,
        superseded_by=replacement.profile_ref,
        reason="POLICY_HARDENED",
        observed_at=NOW + timedelta(seconds=3),
    )

    event = registry.events()[-1]

    assert event.event_type is QualificationProfileEventType.DEPRECATED
    assert event.status is QualificationProfileStatus.DEPRECATED
    assert event.reason == "POLICY_HARDENED"
    assert event.superseded_by == replacement.profile_ref
    assert event.profile_sha256 == old.profile_sha256
    assert registry.verify_event_chain() == event.event_sha256


def test_empty_profile_event_journal_has_genesis_head() -> None:
    registry = QualificationProfileRegistry()

    assert registry.event_count == 0
    assert registry.event_head_sha256 == "0" * 64
    assert registry.verify_event_chain() == "0" * 64
    assert registry.events() == ()
