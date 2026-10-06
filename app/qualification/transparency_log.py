from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime

_SCHEMA_VERSION = "astra-qualification-transparency-log-v1"
_ENTRY_SCHEMA_VERSION = "astra-qualification-transparency-entry-v1"
_HEAD_SCHEMA_VERSION = "astra-qualification-transparency-head-v1"
_GENESIS_HEAD = "0" * 64


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: object) -> str:
    return _sha256_bytes(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    )


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _digest(value: str, name: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{name} must be a sha256 digest")
    return normalized


def _leaf_hash(entry_sha256: str) -> str:
    return _sha256_bytes(b"\x00" + bytes.fromhex(_digest(entry_sha256, "entry_sha256")))


def _node_hash(left_sha256: str, right_sha256: str) -> str:
    return _sha256_bytes(
        b"\x01"
        + bytes.fromhex(_digest(left_sha256, "left_sha256"))
        + bytes.fromhex(_digest(right_sha256, "right_sha256"))
    )


def _merkle_root_from_leaf_hashes(leaf_hashes: tuple[str, ...]) -> str:
    if not leaf_hashes:
        return _GENESIS_HEAD
    level = tuple(_digest(item, "leaf_hash") for item in leaf_hashes)
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


@dataclass(frozen=True)
class QualificationTransparencyEntry:
    entry_type: str
    object_id: str
    object_sha256: str
    subject: str
    subject_version: str
    profile_id: str
    profile_version: str
    published_at: datetime
    schema_version: str = _ENTRY_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != _ENTRY_SCHEMA_VERSION:
            raise ValueError("qualification transparency entry schema mismatch")
        for name, value in (
            ("entry_type", self.entry_type),
            ("object_id", self.object_id),
            ("subject", self.subject),
            ("subject_version", self.subject_version),
            ("profile_id", self.profile_id),
            ("profile_version", self.profile_version),
        ):
            if not value.strip():
                raise ValueError(f"{name} is required")
        _digest(self.object_sha256, "object_sha256")
        _aware(self.published_at, "published_at")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "entry_type": self.entry_type,
            "object_id": self.object_id,
            "object_sha256": self.object_sha256,
            "subject": self.subject,
            "subject_version": self.subject_version,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "published_at": _aware(self.published_at, "published_at").isoformat(),
        }

    @property
    def entry_sha256(self) -> str:
        return _sha256_json(self.payload())

    @property
    def leaf_sha256(self) -> str:
        return _leaf_hash(self.entry_sha256)


@dataclass(frozen=True)
class QualificationTransparencyTreeHead:
    tree_size: int
    root_sha256: str
    previous_tree_head_sha256: str
    issued_at: datetime
    schema_version: str = _HEAD_SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != _HEAD_SCHEMA_VERSION:
            raise ValueError("qualification transparency head schema mismatch")
        if self.tree_size < 0:
            raise ValueError("qualification transparency tree_size must be non-negative")
        _digest(self.root_sha256, "root_sha256")
        _digest(self.previous_tree_head_sha256, "previous_tree_head_sha256")
        _aware(self.issued_at, "issued_at")
        if self.tree_size == 0 and self.root_sha256 != _GENESIS_HEAD:
            raise ValueError("empty qualification transparency tree requires genesis root")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "tree_size": self.tree_size,
            "root_sha256": self.root_sha256,
            "previous_tree_head_sha256": self.previous_tree_head_sha256,
            "issued_at": _aware(self.issued_at, "issued_at").isoformat(),
        }

    @property
    def tree_head_sha256(self) -> str:
        return _sha256_json(self.payload())

    @property
    def tree_head_id(self) -> str:
        return f"qtree_{self.tree_head_sha256[:24]}"


@dataclass(frozen=True)
class QualificationInclusionProof:
    tree_size: int
    leaf_index: int
    leaf_sha256: str
    audit_path: tuple[str, ...]
    root_sha256: str

    def validate(self) -> None:
        if self.tree_size < 1:
            raise ValueError("qualification inclusion proof tree_size must be positive")
        if self.leaf_index < 0 or self.leaf_index >= self.tree_size:
            raise ValueError("qualification inclusion proof leaf_index is out of range")
        _digest(self.leaf_sha256, "leaf_sha256")
        _digest(self.root_sha256, "root_sha256")
        for item in self.audit_path:
            _digest(item, "audit_path item")


@dataclass(frozen=True)
class QualificationDeltaConsistencyProof:
    previous_tree_size: int
    current_tree_size: int
    previous_root_sha256: str
    current_root_sha256: str
    previous_leaf_hashes: tuple[str, ...]
    appended_leaf_hashes: tuple[str, ...]

    def validate(self) -> None:
        if self.previous_tree_size < 0:
            raise ValueError("previous_tree_size must be non-negative")
        if self.current_tree_size < self.previous_tree_size:
            raise ValueError("current_tree_size cannot precede previous_tree_size")
        if len(self.previous_leaf_hashes) != self.previous_tree_size:
            raise ValueError("previous consistency leaf count mismatch")
        if (
            len(self.previous_leaf_hashes) + len(self.appended_leaf_hashes)
            != self.current_tree_size
        ):
            raise ValueError("current consistency leaf count mismatch")
        _digest(self.previous_root_sha256, "previous_root_sha256")
        _digest(self.current_root_sha256, "current_root_sha256")
        for item in (*self.previous_leaf_hashes, *self.appended_leaf_hashes):
            _digest(item, "consistency leaf hash")


class QualificationTransparencyLog:
    """Append-only Merkle transparency log for qualification artifacts."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: list[QualificationTransparencyEntry] = []
        self._entry_ids: set[str] = set()
        self._heads: list[QualificationTransparencyTreeHead] = []

    def append(
        self,
        *,
        entry: QualificationTransparencyEntry,
        issued_at: datetime,
    ) -> QualificationTransparencyTreeHead:
        entry.validate()
        now = _aware(issued_at, "issued_at")
        if now < _aware(entry.published_at, "entry.published_at"):
            raise ValueError("transparency head cannot predate published entry")
        with self._lock:
            if entry.object_id in self._entry_ids:
                raise ValueError("qualification transparency object is already published")
            self._entries.append(entry)
            self._entry_ids.add(entry.object_id)
            previous = (
                self._heads[-1].tree_head_sha256 if self._heads else _GENESIS_HEAD
            )
            root = _merkle_root_from_leaf_hashes(
                tuple(item.leaf_sha256 for item in self._entries)
            )
            head = QualificationTransparencyTreeHead(
                tree_size=len(self._entries),
                root_sha256=root,
                previous_tree_head_sha256=previous,
                issued_at=now,
            )
            head.validate()
            self._heads.append(head)
            return head

    def latest_head(self) -> QualificationTransparencyTreeHead:
        with self._lock:
            if not self._heads:
                return QualificationTransparencyTreeHead(
                    tree_size=0,
                    root_sha256=_GENESIS_HEAD,
                    previous_tree_head_sha256=_GENESIS_HEAD,
                    issued_at=datetime(1970, 1, 1, tzinfo=UTC),
                )
            return self._heads[-1]

    def head_at_size(self, tree_size: int) -> QualificationTransparencyTreeHead:
        if tree_size < 0:
            raise ValueError("qualification transparency tree_size must be non-negative")
        with self._lock:
            if tree_size == 0:
                return QualificationTransparencyTreeHead(
                    tree_size=0,
                    root_sha256=_GENESIS_HEAD,
                    previous_tree_head_sha256=_GENESIS_HEAD,
                    issued_at=datetime(1970, 1, 1, tzinfo=UTC),
                )
            if tree_size > len(self._heads):
                raise ValueError("qualification transparency tree_size is not published")
            return self._heads[tree_size - 1]

    def inclusion_proof(self, *, object_id: str) -> QualificationInclusionProof:
        with self._lock:
            index = next(
                (
                    idx
                    for idx, entry in enumerate(self._entries)
                    if entry.object_id == object_id
                ),
                None,
            )
            if index is None:
                raise ValueError("qualification transparency object is not published")
            leaf_hashes = [item.leaf_sha256 for item in self._entries]
            proof = _build_inclusion_proof(tuple(leaf_hashes), index)
            value = QualificationInclusionProof(
                tree_size=len(leaf_hashes),
                leaf_index=index,
                leaf_sha256=leaf_hashes[index],
                audit_path=proof,
                root_sha256=_merkle_root_from_leaf_hashes(tuple(leaf_hashes)),
            )
            value.validate()
            return value

    def consistency_proof(
        self,
        *,
        previous_tree_size: int,
    ) -> QualificationDeltaConsistencyProof:
        with self._lock:
            if previous_tree_size < 0 or previous_tree_size > len(self._entries):
                raise ValueError("previous_tree_size is out of range")
            all_hashes = tuple(item.leaf_sha256 for item in self._entries)
            previous = all_hashes[:previous_tree_size]
            appended = all_hashes[previous_tree_size:]
            proof = QualificationDeltaConsistencyProof(
                previous_tree_size=previous_tree_size,
                current_tree_size=len(all_hashes),
                previous_root_sha256=_merkle_root_from_leaf_hashes(previous),
                current_root_sha256=_merkle_root_from_leaf_hashes(all_hashes),
                previous_leaf_hashes=previous,
                appended_leaf_hashes=appended,
            )
            proof.validate()
            return proof

    def entries(self) -> tuple[QualificationTransparencyEntry, ...]:
        with self._lock:
            return tuple(self._entries)


def verify_inclusion_proof(proof: QualificationInclusionProof) -> bool:
    proof.validate()
    computed = proof.leaf_sha256
    index = proof.leaf_index
    width = proof.tree_size
    path_index = 0
    while width > 1:
        sibling_exists = (
            index % 2 == 1
            or index + 1 < width
        )
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
    return path_index == len(proof.audit_path) and computed == proof.root_sha256


def verify_delta_consistency(proof: QualificationDeltaConsistencyProof) -> bool:
    proof.validate()
    previous_root = _merkle_root_from_leaf_hashes(proof.previous_leaf_hashes)
    current_root = _merkle_root_from_leaf_hashes(
        proof.previous_leaf_hashes + proof.appended_leaf_hashes
    )
    return (
        previous_root == proof.previous_root_sha256
        and current_root == proof.current_root_sha256
    )


def transparency_root_at_size_from_consistency(
    proof: QualificationDeltaConsistencyProof,
    *,
    tree_size: int,
) -> str:
    proof.validate()
    if tree_size < proof.previous_tree_size or tree_size > proof.current_tree_size:
        raise ValueError("requested transparency tree_size is outside consistency proof")
    all_hashes = proof.previous_leaf_hashes + proof.appended_leaf_hashes
    return _merkle_root_from_leaf_hashes(all_hashes[:tree_size])


def _build_inclusion_proof(
    leaf_hashes: tuple[str, ...],
    leaf_index: int,
) -> tuple[str, ...]:
    if not leaf_hashes:
        raise ValueError("cannot build inclusion proof for empty tree")
    if leaf_index < 0 or leaf_index >= len(leaf_hashes):
        raise ValueError("leaf_index is out of range")

    path: list[str] = []
    level = leaf_hashes
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
