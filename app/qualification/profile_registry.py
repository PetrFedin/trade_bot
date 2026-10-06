from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

_SCHEMA_VERSION = "astra-qualification-profile-v1"
_REGISTRY_SCHEMA_VERSION = "astra-qualification-profile-registry-v2"
_GENESIS = "0" * 64


class QualificationProfileStatus(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"
    REVOKED = "REVOKED"


@dataclass(frozen=True)
class QualificationProfile:
    profile_id: str
    version: str
    scope: str
    allowed_environments: tuple[str, ...]
    required_corpus_classes: tuple[str, ...]
    required_checks: tuple[str, ...]
    required_assertions: tuple[str, ...]
    required_limitations: tuple[str, ...]
    created_at: datetime
    schema_version: str = _SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError("qualification profile schema mismatch")
        for name, value in (
            ("profile_id", self.profile_id),
            ("version", self.version),
            ("scope", self.scope),
        ):
            if not value.strip():
                raise ValueError(f"{name} is required")
        _aware(self.created_at, "created_at")
        for name, values in (
            ("allowed_environments", self.allowed_environments),
            ("required_corpus_classes", self.required_corpus_classes),
            ("required_checks", self.required_checks),
            ("required_assertions", self.required_assertions),
            ("required_limitations", self.required_limitations),
        ):
            if not values:
                raise ValueError(f"{name} cannot be empty")
            if len(set(values)) != len(values):
                raise ValueError(f"{name} must be unique")
            if any(not item.strip() for item in values):
                raise ValueError(f"{name} cannot contain blank values")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "version": self.version,
            "scope": self.scope,
            "allowed_environments": list(self.allowed_environments),
            "required_corpus_classes": list(self.required_corpus_classes),
            "required_checks": list(self.required_checks),
            "required_assertions": list(self.required_assertions),
            "required_limitations": list(self.required_limitations),
            "created_at": _aware(self.created_at, "created_at").isoformat(),
        }

    @property
    def profile_sha256(self) -> str:
        return _sha256(self.payload())

    @property
    def profile_ref(self) -> str:
        return f"{self.profile_id}@{self.version}"


@dataclass(frozen=True)
class QualificationProfileRecord:
    profile: QualificationProfile
    status: QualificationProfileStatus
    registered_at: datetime
    updated_at: datetime
    lifecycle_reason: str | None = None
    superseded_by: str | None = None

    def validate(self) -> None:
        self.profile.validate()
        registered = _aware(self.registered_at, "registered_at")
        updated = _aware(self.updated_at, "updated_at")
        if updated < registered:
            raise ValueError("qualification profile updated_at cannot precede registered_at")
        if self.status in {QualificationProfileStatus.DRAFT, QualificationProfileStatus.ACTIVE}:
            if self.lifecycle_reason is not None or self.superseded_by is not None:
                raise ValueError("open qualification profile cannot carry lifecycle closure")
        elif self.status is QualificationProfileStatus.DEPRECATED:
            if not (self.lifecycle_reason or "").strip():
                raise ValueError("deprecated qualification profile requires a reason")
            if not (self.superseded_by or "").strip():
                raise ValueError("deprecated qualification profile requires superseded_by")
        elif self.status is QualificationProfileStatus.REVOKED:
            if not (self.lifecycle_reason or "").strip():
                raise ValueError("revoked qualification profile requires a reason")
            if self.superseded_by is not None:
                raise ValueError("revoked qualification profile cannot carry superseded_by")

    def state_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "profile_ref": self.profile.profile_ref,
            "profile_sha256": self.profile.profile_sha256,
            "status": self.status.value,
            "registered_at": _aware(self.registered_at, "registered_at").isoformat(),
            "updated_at": _aware(self.updated_at, "updated_at").isoformat(),
            "lifecycle_reason": self.lifecycle_reason,
            "superseded_by": self.superseded_by,
        }

    @property
    def state_leaf_sha256(self) -> str:
        return _leaf_hash(_sha256(self.state_payload()))


@dataclass(frozen=True)
class QualificationProfileStateProof:
    profile_ref: str
    record: QualificationProfileRecord
    leaf_index: int
    tree_size: int
    audit_path: tuple[str, ...]
    state_root_sha256: str
    schema_version: str = _REGISTRY_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != _REGISTRY_SCHEMA_VERSION:
            raise ValueError("qualification profile state proof schema mismatch")
        if self.profile_ref != self.record.profile.profile_ref:
            raise ValueError("qualification profile state proof identity mismatch")
        if self.tree_size < 1:
            raise ValueError("qualification profile state proof tree_size must be positive")
        if self.leaf_index < 0 or self.leaf_index >= self.tree_size:
            raise ValueError("qualification profile state proof leaf_index is out of range")
        self.record.validate()
        _digest(self.state_root_sha256, "state_root_sha256")
        for item in self.audit_path:
            _digest(item, "audit_path item")


class QualificationProfileRegistry:
    """In-memory authority for immutable profile definitions and lifecycle."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, QualificationProfileRecord] = {}

    def register(
        self,
        *,
        profile: QualificationProfile,
        observed_at: datetime,
    ) -> QualificationProfileRecord:
        profile.validate()
        now = _aware(observed_at, "observed_at")
        if now < _aware(profile.created_at, "profile.created_at"):
            raise ValueError("qualification profile cannot be registered before creation")
        ref = profile.profile_ref
        with self._lock:
            existing = self._records.get(ref)
            if existing is not None:
                if existing.profile.profile_sha256 != profile.profile_sha256:
                    raise ValueError("qualification profile version is immutable")
                raise ValueError("qualification profile is already registered")
            record = QualificationProfileRecord(
                profile=profile,
                status=QualificationProfileStatus.DRAFT,
                registered_at=now,
                updated_at=now,
            )
            record.validate()
            self._records[ref] = record
            return record

    def activate(
        self,
        *,
        profile_ref: str,
        observed_at: datetime,
    ) -> QualificationProfileRecord:
        now = _aware(observed_at, "observed_at")
        with self._lock:
            record = self._require(profile_ref)
            if record.status is not QualificationProfileStatus.DRAFT:
                raise ValueError("only DRAFT qualification profile can be activated")
            if now < record.updated_at:
                raise ValueError("qualification profile lifecycle time regression")
            updated = QualificationProfileRecord(
                profile=record.profile,
                status=QualificationProfileStatus.ACTIVE,
                registered_at=record.registered_at,
                updated_at=now,
            )
            updated.validate()
            self._records[profile_ref] = updated
            return updated

    def deprecate(
        self,
        *,
        profile_ref: str,
        superseded_by: str,
        reason: str,
        observed_at: datetime,
    ) -> QualificationProfileRecord:
        if not reason.strip():
            raise ValueError("qualification profile deprecation reason is required")
        now = _aware(observed_at, "observed_at")
        with self._lock:
            record = self._require(profile_ref)
            replacement = self._require(superseded_by)
            if record.status is not QualificationProfileStatus.ACTIVE:
                raise ValueError("only ACTIVE qualification profile can be deprecated")
            if replacement.status is not QualificationProfileStatus.ACTIVE:
                raise ValueError("replacement qualification profile must be ACTIVE")
            if replacement.profile.profile_id != record.profile.profile_id:
                raise ValueError("replacement qualification profile family mismatch")
            if replacement.profile.profile_ref == record.profile.profile_ref:
                raise ValueError("qualification profile cannot supersede itself")
            if now < record.updated_at or now < replacement.updated_at:
                raise ValueError("qualification profile lifecycle time regression")
            updated = QualificationProfileRecord(
                profile=record.profile,
                status=QualificationProfileStatus.DEPRECATED,
                registered_at=record.registered_at,
                updated_at=now,
                lifecycle_reason=reason,
                superseded_by=replacement.profile.profile_ref,
            )
            updated.validate()
            self._records[profile_ref] = updated
            return updated

    def revoke(
        self,
        *,
        profile_ref: str,
        reason: str,
        observed_at: datetime,
    ) -> QualificationProfileRecord:
        if not reason.strip():
            raise ValueError("qualification profile revocation reason is required")
        now = _aware(observed_at, "observed_at")
        with self._lock:
            record = self._require(profile_ref)
            if record.status not in {
                QualificationProfileStatus.DRAFT,
                QualificationProfileStatus.ACTIVE,
            }:
                raise ValueError("qualification profile is already closed")
            if now < record.updated_at:
                raise ValueError("qualification profile lifecycle time regression")
            updated = QualificationProfileRecord(
                profile=record.profile,
                status=QualificationProfileStatus.REVOKED,
                registered_at=record.registered_at,
                updated_at=now,
                lifecycle_reason=reason,
            )
            updated.validate()
            self._records[profile_ref] = updated
            return updated

    def require_active(
        self,
        *,
        profile_id: str,
        version: str,
        environment: str,
        corpus_classes: tuple[str, ...],
        observed_at: datetime,
    ) -> QualificationProfile:
        _aware(observed_at, "observed_at")
        ref = f"{profile_id}@{version}"
        with self._lock:
            record = self._require(ref)
            if record.status is not QualificationProfileStatus.ACTIVE:
                raise ValueError("qualification profile is not ACTIVE")
            profile = record.profile
            if environment not in profile.allowed_environments:
                raise ValueError("qualification environment is not allowed by profile")
            available = set(corpus_classes)
            missing = set(profile.required_corpus_classes) - available
            if missing:
                raise ValueError(
                    "qualification corpus classes missing: "
                    + ",".join(sorted(missing))
                )
            return profile

    @property
    def state_root_sha256(self) -> str:
        with self._lock:
            return _profile_state_root(self._records)

    @property
    def record_count(self) -> int:
        with self._lock:
            return len(self._records)

    @property
    def latest_updated_at(self) -> datetime:
        with self._lock:
            if not self._records:
                return datetime(1970, 1, 1, tzinfo=UTC)
            return max(record.updated_at for record in self._records.values())

    def state_proof(self, *, profile_ref: str) -> QualificationProfileStateProof:
        with self._lock:
            ordered = tuple(
                sorted(
                    self._records.values(),
                    key=lambda record: record.profile.profile_ref,
                )
            )
            index = next(
                (
                    position
                    for position, record in enumerate(ordered)
                    if record.profile.profile_ref == profile_ref
                ),
                None,
            )
            if index is None:
                raise ValueError("qualification profile is not registered")
            leaves = tuple(record.state_leaf_sha256 for record in ordered)
            proof = QualificationProfileStateProof(
                profile_ref=profile_ref,
                record=ordered[index],
                leaf_index=index,
                tree_size=len(leaves),
                audit_path=_merkle_path(leaves, index),
                state_root_sha256=_merkle_root(leaves),
            )
            proof.validate()
            return proof

    def get(self, profile_ref: str) -> QualificationProfileRecord | None:
        with self._lock:
            return self._records.get(profile_ref)

    def active_versions(self, profile_id: str) -> tuple[QualificationProfile, ...]:
        with self._lock:
            return tuple(
                record.profile
                for record in self._records.values()
                if record.profile.profile_id == profile_id
                and record.status is QualificationProfileStatus.ACTIVE
            )

    def _require(self, profile_ref: str) -> QualificationProfileRecord:
        record = self._records.get(profile_ref)
        if record is None:
            raise ValueError("qualification profile is not registered")
        return record


def verify_profile_state_proof(proof: QualificationProfileStateProof) -> bool:
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


def _profile_state_root(
    records: dict[str, QualificationProfileRecord],
) -> str:
    ordered = tuple(
        sorted(
            records.values(),
            key=lambda record: record.profile.profile_ref,
        )
    )
    return _merkle_root(tuple(record.state_leaf_sha256 for record in ordered))


def _leaf_hash(payload_sha256: str) -> str:
    return hashlib.sha256(
        b"\x00" + bytes.fromhex(_digest(payload_sha256, "payload_sha256"))
    ).hexdigest()


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


def _merkle_path(
    leaves: tuple[str, ...],
    leaf_index: int,
) -> tuple[str, ...]:
    if not leaves or leaf_index < 0 or leaf_index >= len(leaves):
        raise ValueError("invalid qualification profile state proof leaf")
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
