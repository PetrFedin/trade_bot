from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from app.qualification.profile_registry import (
    QualificationProfileEvent,
    QualificationProfileRegistry,
)
from app.qualification.transparency_log import (
    QualificationTransparencyEntry,
    QualificationTransparencyLog,
    QualificationTransparencyTreeHead,
)

_ENTRY_TYPE = "QUALIFICATION_PROFILE_EVENT"
_RECEIPT_SCHEMA_VERSION = "astra-qualification-profile-publication-receipt-v1"
_GENESIS = "0" * 64


@dataclass(frozen=True)
class QualificationProfilePublicationReceipt:
    profile_event_count: int
    profile_event_head_sha256: str
    published_profile_event_count: int
    transparency_tree_size: int
    transparency_root_sha256: str
    transparency_tree_head_sha256: str
    observed_at: datetime
    schema_version: str = _RECEIPT_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != _RECEIPT_SCHEMA_VERSION:
            raise ValueError("profile publication receipt schema mismatch")
        if self.profile_event_count < 0:
            raise ValueError("profile_event_count must be non-negative")
        if self.published_profile_event_count < 0:
            raise ValueError("published_profile_event_count must be non-negative")
        if self.published_profile_event_count != self.profile_event_count:
            raise ValueError("profile lifecycle publication is incomplete")
        if self.transparency_tree_size < 0:
            raise ValueError("transparency_tree_size must be non-negative")
        for name, value in (
            ("profile_event_head_sha256", self.profile_event_head_sha256),
            ("transparency_root_sha256", self.transparency_root_sha256),
            ("transparency_tree_head_sha256", self.transparency_tree_head_sha256),
        ):
            _digest(value, name)
        _aware(self.observed_at, "observed_at")
        if self.profile_event_count == 0 and self.profile_event_head_sha256 != _GENESIS:
            raise ValueError("empty profile event journal requires genesis head")
        if self.transparency_tree_size == 0 and self.transparency_root_sha256 != _GENESIS:
            raise ValueError("empty transparency tree requires genesis root")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "profile_event_count": self.profile_event_count,
            "profile_event_head_sha256": self.profile_event_head_sha256,
            "published_profile_event_count": self.published_profile_event_count,
            "transparency_tree_size": self.transparency_tree_size,
            "transparency_root_sha256": self.transparency_root_sha256,
            "transparency_tree_head_sha256": self.transparency_tree_head_sha256,
            "observed_at": _aware(self.observed_at, "observed_at").isoformat(),
        }

    @property
    def receipt_sha256(self) -> str:
        return _sha256(self.payload())

    @property
    def receipt_id(self) -> str:
        return f"qprofilepub_{self.receipt_sha256[:24]}"


def profile_event_object_id(event: QualificationProfileEvent) -> str:
    event.validate()
    return f"qprofileevent_{event.sequence}_{event.event_sha256[:24]}"


def profile_event_transparency_entry(
    *,
    event: QualificationProfileEvent,
    published_at: datetime,
) -> QualificationTransparencyEntry:
    event.validate()
    publication_time = _aware(published_at, "published_at")
    if publication_time < _aware(event.observed_at, "event.observed_at"):
        raise ValueError("profile lifecycle event cannot be published before observation")
    profile_id, profile_version = _split_profile_ref(event.profile_ref)
    entry = QualificationTransparencyEntry(
        entry_type=_ENTRY_TYPE,
        object_id=profile_event_object_id(event),
        object_sha256=event.event_sha256,
        subject="QUALIFICATION_PROFILE",
        subject_version=event.event_type.value,
        profile_id=profile_id,
        profile_version=profile_version,
        published_at=publication_time,
    )
    entry.validate()
    return entry


def publish_profile_lifecycle(
    *,
    profile_registry: QualificationProfileRegistry,
    transparency_log: QualificationTransparencyLog,
    observed_at: datetime,
) -> QualificationProfilePublicationReceipt:
    now = _aware(observed_at, "observed_at")
    events = profile_registry.events()
    verified_head = profile_registry.verify_event_chain()
    if len(events) != profile_registry.event_count:
        raise ValueError("profile lifecycle event count changed during publication")
    if verified_head != profile_registry.event_head_sha256:
        raise ValueError("profile lifecycle event head changed during publication")
    if events and now < _aware(events[-1].observed_at, "latest profile event"):
        raise ValueError("profile lifecycle publication cannot predate latest event")

    published = tuple(
        entry
        for entry in transparency_log.entries()
        if entry.entry_type == _ENTRY_TYPE
    )
    if len(published) > len(events):
        raise ValueError("published profile lifecycle exceeds authoritative event journal")

    for index, entry in enumerate(published):
        _verify_published_event(entry=entry, event=events[index])

    for event in events[len(published) :]:
        entry = profile_event_transparency_entry(
            event=event,
            published_at=now,
        )
        transparency_log.append(entry=entry, issued_at=now)

    final_profile_entries = tuple(
        entry
        for entry in transparency_log.entries()
        if entry.entry_type == _ENTRY_TYPE
    )
    if len(final_profile_entries) != len(events):
        raise ValueError("profile lifecycle transparency publication is incomplete")
    for index, entry in enumerate(final_profile_entries):
        _verify_published_event(entry=entry, event=events[index])

    head = transparency_log.latest_head()
    receipt = QualificationProfilePublicationReceipt(
        profile_event_count=len(events),
        profile_event_head_sha256=verified_head,
        published_profile_event_count=len(final_profile_entries),
        transparency_tree_size=head.tree_size,
        transparency_root_sha256=head.root_sha256,
        transparency_tree_head_sha256=head.tree_head_sha256,
        observed_at=now,
    )
    receipt.validate()
    return receipt


def verify_profile_publication_receipt(
    *,
    receipt: QualificationProfilePublicationReceipt,
    profile_registry: QualificationProfileRegistry,
    transparency_log: QualificationTransparencyLog,
) -> bool:
    receipt.validate()
    if profile_registry.verify_event_chain() != receipt.profile_event_head_sha256:
        return False
    if profile_registry.event_count != receipt.profile_event_count:
        return False
    published = tuple(
        entry
        for entry in transparency_log.entries()
        if entry.entry_type == _ENTRY_TYPE
    )
    if len(published) != receipt.published_profile_event_count:
        return False
    events = profile_registry.events()
    if len(events) != len(published):
        return False
    try:
        for index, entry in enumerate(published):
            _verify_published_event(entry=entry, event=events[index])
    except ValueError:
        return False
    head = transparency_log.latest_head()
    return (
        head.tree_size == receipt.transparency_tree_size
        and head.root_sha256 == receipt.transparency_root_sha256
        and head.tree_head_sha256 == receipt.transparency_tree_head_sha256
    )


def _verify_published_event(
    *,
    entry: QualificationTransparencyEntry,
    event: QualificationProfileEvent,
) -> None:
    entry.validate()
    event.validate()
    profile_id, profile_version = _split_profile_ref(event.profile_ref)
    if entry.entry_type != _ENTRY_TYPE:
        raise ValueError("profile lifecycle transparency entry type mismatch")
    if entry.object_id != profile_event_object_id(event):
        raise ValueError("profile lifecycle transparency object id mismatch")
    if entry.object_sha256 != event.event_sha256:
        raise ValueError("profile lifecycle transparency event digest mismatch")
    if entry.subject != "QUALIFICATION_PROFILE":
        raise ValueError("profile lifecycle transparency subject mismatch")
    if entry.subject_version != event.event_type.value:
        raise ValueError("profile lifecycle transparency event type mismatch")
    if entry.profile_id != profile_id or entry.profile_version != profile_version:
        raise ValueError("profile lifecycle transparency profile identity mismatch")
    if _aware(entry.published_at, "entry.published_at") < _aware(
        event.observed_at,
        "event.observed_at",
    ):
        raise ValueError("profile lifecycle transparency publication predates event")


def _split_profile_ref(profile_ref: str) -> tuple[str, str]:
    profile_id, separator, version = profile_ref.rpartition("@")
    if not separator or not profile_id.strip() or not version.strip():
        raise ValueError("qualification profile_ref must be profile_id@version")
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
