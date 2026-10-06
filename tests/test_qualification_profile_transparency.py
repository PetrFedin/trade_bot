from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from app.qualification.profile_registry import (
    QualificationProfileEvent,
    QualificationProfileEventType,
    QualificationProfileRegistry,
    QualificationProfileStatus,
)
from app.qualification.profile_transparency import (
    QualificationProfilePublicationReceipt,
    profile_event_object_id,
    profile_event_transparency_entry,
    publish_profile_lifecycle,
    verify_profile_publication_receipt,
)
from app.qualification.transparency_log import (
    QualificationTransparencyEntry,
    QualificationTransparencyLog,
)
from tests.test_qualification_profile_registry import NOW, profile


def populated_profile_registry() -> QualificationProfileRegistry:
    registry = QualificationProfileRegistry()
    first = profile(version="9.0.0")
    second = profile(version="9.1.0")
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


def unrelated_entry(*, suffix: str, published_at):
    return QualificationTransparencyEntry(
        entry_type="SIGNED_QUALIFICATION_EVIDENCE",
        object_id=f"qevidence-{suffix}",
        object_sha256="a" * 64,
        subject="BYBIT_PUBLIC_MARKETDATA_ADAPTER",
        subject_version=f"build-{suffix}",
        profile_id="ASTRA_BYBIT_PUBLIC_MARKETDATA",
        profile_version="9.1.0",
        published_at=published_at,
    )


def test_publish_profile_lifecycle_publishes_exact_journal_prefix() -> None:
    registry = populated_profile_registry()
    log = QualificationTransparencyLog()

    receipt = publish_profile_lifecycle(
        profile_registry=registry,
        transparency_log=log,
        observed_at=NOW + timedelta(seconds=4),
    )

    events = registry.events()
    published = tuple(
        entry
        for entry in log.entries()
        if entry.entry_type == "QUALIFICATION_PROFILE_EVENT"
    )
    assert receipt.profile_event_count == len(events)
    assert receipt.profile_event_head_sha256 == registry.event_head_sha256
    assert receipt.published_profile_event_count == len(events)
    assert len(published) == len(events)
    assert tuple(entry.object_id for entry in published) == tuple(
        profile_event_object_id(event) for event in events
    )
    assert tuple(entry.object_sha256 for entry in published) == tuple(
        event.event_sha256 for event in events
    )
    assert verify_profile_publication_receipt(
        receipt=receipt,
        profile_registry=registry,
        transparency_log=log,
    )


def test_profile_lifecycle_publication_is_restart_safe_and_idempotent() -> None:
    registry = populated_profile_registry()
    log = QualificationTransparencyLog()

    first = publish_profile_lifecycle(
        profile_registry=registry,
        transparency_log=log,
        observed_at=NOW + timedelta(seconds=4),
    )
    first_size = log.latest_head().tree_size
    second = publish_profile_lifecycle(
        profile_registry=registry,
        transparency_log=log,
        observed_at=NOW + timedelta(seconds=5),
    )

    assert log.latest_head().tree_size == first_size
    assert second.profile_event_count == first.profile_event_count
    assert second.profile_event_head_sha256 == first.profile_event_head_sha256
    assert second.transparency_root_sha256 == first.transparency_root_sha256
    assert second.transparency_tree_head_sha256 == first.transparency_tree_head_sha256


def test_profile_lifecycle_publication_resumes_from_valid_partial_prefix() -> None:
    registry = populated_profile_registry()
    log = QualificationTransparencyLog()
    events = registry.events()
    first_entry = profile_event_transparency_entry(
        event=events[0],
        published_at=NOW + timedelta(seconds=4),
    )
    log.append(
        entry=first_entry,
        issued_at=NOW + timedelta(seconds=4),
    )

    receipt = publish_profile_lifecycle(
        profile_registry=registry,
        transparency_log=log,
        observed_at=NOW + timedelta(seconds=5),
    )

    profile_entries = tuple(
        entry
        for entry in log.entries()
        if entry.entry_type == "QUALIFICATION_PROFILE_EVENT"
    )
    assert len(profile_entries) == len(events)
    assert profile_entries[0] == first_entry
    assert receipt.published_profile_event_count == len(events)
    assert verify_profile_publication_receipt(
        receipt=receipt,
        profile_registry=registry,
        transparency_log=log,
    )


def test_profile_event_prefix_ignores_interleaved_non_profile_entries() -> None:
    registry = populated_profile_registry()
    log = QualificationTransparencyLog()
    events = registry.events()
    first = profile_event_transparency_entry(
        event=events[0],
        published_at=NOW + timedelta(seconds=4),
    )
    log.append(entry=first, issued_at=NOW + timedelta(seconds=4))
    log.append(
        entry=unrelated_entry(
            suffix="interleaved",
            published_at=NOW + timedelta(seconds=4),
        ),
        issued_at=NOW + timedelta(seconds=4),
    )

    receipt = publish_profile_lifecycle(
        profile_registry=registry,
        transparency_log=log,
        observed_at=NOW + timedelta(seconds=5),
    )

    assert receipt.profile_event_count == len(events)
    assert receipt.transparency_tree_size == len(events) + 1
    assert verify_profile_publication_receipt(
        receipt=receipt,
        profile_registry=registry,
        transparency_log=log,
    )


def test_profile_lifecycle_publication_rejects_corrupted_prefix() -> None:
    registry = populated_profile_registry()
    log = QualificationTransparencyLog()
    event = registry.events()[0]
    legitimate = profile_event_transparency_entry(
        event=event,
        published_at=NOW + timedelta(seconds=4),
    )
    corrupted = replace(
        legitimate,
        object_sha256="f" * 64,
    )
    corrupted.validate()
    log.append(
        entry=corrupted,
        issued_at=NOW + timedelta(seconds=4),
    )

    with pytest.raises(ValueError, match="event digest mismatch"):
        publish_profile_lifecycle(
            profile_registry=registry,
            transparency_log=log,
            observed_at=NOW + timedelta(seconds=5),
        )


def test_profile_publication_receipt_remains_valid_after_log_grows() -> None:
    registry = populated_profile_registry()
    log = QualificationTransparencyLog()
    receipt = publish_profile_lifecycle(
        profile_registry=registry,
        transparency_log=log,
        observed_at=NOW + timedelta(seconds=4),
    )

    log.append(
        entry=unrelated_entry(
            suffix="later",
            published_at=NOW + timedelta(seconds=5),
        ),
        issued_at=NOW + timedelta(seconds=5),
    )

    assert log.latest_head().tree_size > receipt.transparency_tree_size
    assert verify_profile_publication_receipt(
        receipt=receipt,
        profile_registry=registry,
        transparency_log=log,
    )


def test_profile_publication_receipt_fails_after_profile_journal_advances() -> None:
    registry = populated_profile_registry()
    log = QualificationTransparencyLog()
    receipt = publish_profile_lifecycle(
        profile_registry=registry,
        transparency_log=log,
        observed_at=NOW + timedelta(seconds=4),
    )
    latest = profile(version="9.2.0")
    registry.register(
        profile=latest,
        observed_at=NOW + timedelta(seconds=5),
    )

    assert not verify_profile_publication_receipt(
        receipt=receipt,
        profile_registry=registry,
        transparency_log=log,
    )


def test_profile_event_cannot_be_published_before_observation() -> None:
    registry = populated_profile_registry()
    event = registry.events()[-1]

    with pytest.raises(ValueError, match="before observation"):
        profile_event_transparency_entry(
            event=event,
            published_at=event.observed_at - timedelta(seconds=1),
        )


def test_transparency_historical_head_lookup_is_stable() -> None:
    log = QualificationTransparencyLog()
    first = unrelated_entry(suffix="one", published_at=NOW)
    second = unrelated_entry(
        suffix="two",
        published_at=NOW + timedelta(seconds=1),
    )
    first_head = log.append(entry=first, issued_at=NOW)
    log.append(
        entry=second,
        issued_at=NOW + timedelta(seconds=1),
    )

    restored = log.head_at_size(1)
    assert restored == first_head

    with pytest.raises(ValueError, match="not published"):
        log.head_at_size(3)



def test_profile_publication_rejects_registry_mutation_during_append() -> None:
    registry = populated_profile_registry()
    late_profile = profile(version="9.3.0")

    class MutatingTransparencyLog(QualificationTransparencyLog):
        def __init__(self) -> None:
            super().__init__()
            self._mutated = False

        def append(self, *, entry, issued_at):
            head = super().append(entry=entry, issued_at=issued_at)
            if (
                not self._mutated
                and entry.entry_type == "QUALIFICATION_PROFILE_EVENT"
            ):
                self._mutated = True
                registry.register(
                    profile=late_profile,
                    observed_at=NOW + timedelta(seconds=4),
                )
            return head

    log = MutatingTransparencyLog()

    with pytest.raises(
        ValueError,
        match="changed during transparency publication",
    ):
        publish_profile_lifecycle(
            profile_registry=registry,
            transparency_log=log,
            observed_at=NOW + timedelta(seconds=5),
        )

    profile_entries = tuple(
        entry
        for entry in log.entries()
        if entry.entry_type == "QUALIFICATION_PROFILE_EVENT"
    )
    assert profile_entries
    assert len(profile_entries) < registry.event_count



def test_profile_publication_receipt_is_hash_addressed_and_fail_closed() -> None:
    registry = populated_profile_registry()
    log = QualificationTransparencyLog()
    receipt = publish_profile_lifecycle(
        profile_registry=registry,
        transparency_log=log,
        observed_at=NOW + timedelta(seconds=4),
    )

    assert receipt.receipt_id.startswith("qprofilepub_")
    assert receipt.receipt_sha256 == receipt.receipt_sha256
    assert receipt.payload()["profile_event_count"] == registry.event_count

    with pytest.raises(ValueError, match="publication is incomplete"):
        replace(
            receipt,
            published_profile_event_count=receipt.profile_event_count - 1,
        ).validate()

    with pytest.raises(ValueError, match="must be non-negative"):
        replace(receipt, profile_event_count=-1).validate()

    with pytest.raises(ValueError, match="requires genesis head"):
        QualificationProfilePublicationReceipt(
            profile_event_count=0,
            profile_event_head_sha256="f" * 64,
            published_profile_event_count=0,
            transparency_tree_size=0,
            transparency_root_sha256="0" * 64,
            transparency_tree_head_sha256="0" * 64,
            observed_at=NOW,
        ).validate()

    with pytest.raises(ValueError, match="requires genesis root"):
        QualificationProfilePublicationReceipt(
            profile_event_count=0,
            profile_event_head_sha256="0" * 64,
            published_profile_event_count=0,
            transparency_tree_size=0,
            transparency_root_sha256="f" * 64,
            transparency_tree_head_sha256="0" * 64,
            observed_at=NOW,
        ).validate()


def test_profile_publication_rejects_extra_published_event_beyond_journal() -> None:
    registry = QualificationProfileRegistry()
    log = QualificationTransparencyLog()
    log.append(
        entry=QualificationTransparencyEntry(
            entry_type="QUALIFICATION_PROFILE_EVENT",
            object_id="qprofileevent_1_deadbeefdeadbeefdeadbeef",
            object_sha256="a" * 64,
            subject="QUALIFICATION_PROFILE",
            subject_version="REGISTERED",
            profile_id="ASTRA_BYBIT_PUBLIC_MARKETDATA",
            profile_version="99.0.0",
            published_at=NOW,
        ),
        issued_at=NOW,
    )

    with pytest.raises(ValueError, match="exceeds authoritative event journal"):
        publish_profile_lifecycle(
            profile_registry=registry,
            transparency_log=log,
            observed_at=NOW,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("object_id", "qprofileevent_wrong", "object id mismatch"),
        ("subject", "OTHER_SUBJECT", "subject mismatch"),
        ("subject_version", "REVOKED", "event type mismatch"),
        ("profile_id", "OTHER_PROFILE", "profile identity mismatch"),
        ("profile_version", "99.0.0", "profile identity mismatch"),
    ),
)
def test_profile_publication_rejects_corrupted_prefix_identity(
    field: str,
    value: str,
    message: str,
) -> None:
    registry = populated_profile_registry()
    log = QualificationTransparencyLog()
    event = registry.events()[0]
    legitimate = profile_event_transparency_entry(
        event=event,
        published_at=NOW + timedelta(seconds=4),
    )
    corrupted = replace(legitimate, **{field: value})
    corrupted.validate()
    log.append(entry=corrupted, issued_at=NOW + timedelta(seconds=4))

    with pytest.raises(ValueError, match=message):
        publish_profile_lifecycle(
            profile_registry=registry,
            transparency_log=log,
            observed_at=NOW + timedelta(seconds=5),
        )


def test_profile_publication_receipt_detects_tampered_historical_anchor() -> None:
    registry = populated_profile_registry()
    log = QualificationTransparencyLog()
    receipt = publish_profile_lifecycle(
        profile_registry=registry,
        transparency_log=log,
        observed_at=NOW + timedelta(seconds=4),
    )

    assert not verify_profile_publication_receipt(
        receipt=replace(receipt, transparency_root_sha256="f" * 64),
        profile_registry=registry,
        transparency_log=log,
    )
    assert not verify_profile_publication_receipt(
        receipt=replace(
            receipt,
            transparency_tree_size=receipt.transparency_tree_size + 100,
        ),
        profile_registry=registry,
        transparency_log=log,
    )


def test_profile_event_entry_rejects_invalid_profile_ref() -> None:
    event = QualificationProfileEvent(
        sequence=1,
        event_type=QualificationProfileEventType.REGISTERED,
        profile_ref="missing-version-separator",
        profile_sha256="a" * 64,
        status=QualificationProfileStatus.DRAFT,
        observed_at=NOW,
        previous_event_sha256="0" * 64,
    )
    event.validate()

    with pytest.raises(ValueError, match="profile_id@version"):
        profile_event_transparency_entry(
            event=event,
            published_at=NOW,
        )


def test_historical_transparency_head_supports_genesis_and_rejects_negative_size() -> None:
    log = QualificationTransparencyLog()

    genesis = log.head_at_size(0)
    assert genesis.tree_size == 0
    assert genesis.root_sha256 == "0" * 64

    with pytest.raises(ValueError, match="must be non-negative"):
        log.head_at_size(-1)
