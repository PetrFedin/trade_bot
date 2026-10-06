from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.qualification.profile_registry import (
    QualificationProfile,
    QualificationProfileRegistry,
    QualificationProfileStatus,
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
