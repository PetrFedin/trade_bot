from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator, NoReturn

from app.qualification.portable_artifact_codec import canonical_json_bytes
from app.qualification.verification_service_v4 import QualificationTrustStateV4

_AUTHORITY_SCHEMA = "astra-persistent-trust-state-authority-v1"
_RECORD_SCHEMA = "astra-persistent-trust-state-record-v1"
_CURRENT_SCHEMA = "astra-persistent-trust-state-current-v1"
_RECEIPT_SCHEMA = "astra-persistent-trust-state-receipt-v1"
_GENESIS = "0" * 64


class PersistentTrustStateAuthorityError(RuntimeError):
    pass


class PersistentTrustStateCASMismatch(PersistentTrustStateAuthorityError):
    pass


class PersistentTrustStateCorruption(PersistentTrustStateAuthorityError):
    pass


@dataclass(frozen=True)
class TrustStateTransitionContext:
    artifact_id: str
    artifact_sha256: str
    bundle_id: str
    bundle_sha256: str
    checkpoint_v4_id: str
    checkpoint_v4_sha256: str
    verified_at: datetime

    def validate(self) -> None:
        for name, value in (
            ("artifact_id", self.artifact_id),
            ("bundle_id", self.bundle_id),
            ("checkpoint_v4_id", self.checkpoint_v4_id),
        ):
            if not value.strip():
                raise ValueError(f"{name} is required")
        _digest(self.artifact_sha256, "artifact_sha256")
        _digest(self.bundle_sha256, "bundle_sha256")
        _digest(self.checkpoint_v4_sha256, "checkpoint_v4_sha256")
        _aware(self.verified_at, "verified_at")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "artifact_id": self.artifact_id,
            "artifact_sha256": self.artifact_sha256,
            "bundle_id": self.bundle_id,
            "bundle_sha256": self.bundle_sha256,
            "checkpoint_v4_id": self.checkpoint_v4_id,
            "checkpoint_v4_sha256": self.checkpoint_v4_sha256,
            "verified_at": _aware(self.verified_at, "verified_at").isoformat(),
        }


@dataclass(frozen=True)
class PersistentTrustStateRecord:
    generation: int
    previous_record_sha256: str
    trust_state: QualificationTrustStateV4
    trust_state_sha256: str
    transition: TrustStateTransitionContext
    record_sha256: str
    schema_version: str = _RECORD_SCHEMA
    authority_version: str = _AUTHORITY_SCHEMA

    def validate(self) -> None:
        if self.schema_version != _RECORD_SCHEMA:
            raise ValueError("persistent TrustState record schema mismatch")
        if self.authority_version != _AUTHORITY_SCHEMA:
            raise ValueError("persistent TrustState authority version mismatch")
        if self.generation < 0:
            raise ValueError("persistent TrustState generation must be non-negative")
        _digest(self.previous_record_sha256, "previous_record_sha256")
        self.trust_state.validate()
        self.transition.validate()
        expected_state_sha = _sha256_json(self.trust_state.payload())
        if self.trust_state_sha256 != expected_state_sha:
            raise ValueError("persistent TrustState state digest mismatch")
        expected_record_sha = _sha256_json(self.unsigned_payload())
        if self.record_sha256 != expected_record_sha:
            raise ValueError("persistent TrustState record digest mismatch")

    def unsigned_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "authority_version": self.authority_version,
            "generation": self.generation,
            "previous_record_sha256": self.previous_record_sha256,
            "trust_state": self.trust_state.payload(),
            "trust_state_sha256": self.trust_state_sha256,
            "transition": self.transition.payload(),
        }

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            **self.unsigned_payload(),
            "record_sha256": self.record_sha256,
        }

    @classmethod
    def build(
        cls,
        *,
        generation: int,
        previous_record_sha256: str,
        trust_state: QualificationTrustStateV4,
        transition: TrustStateTransitionContext,
    ) -> "PersistentTrustStateRecord":
        trust_state.validate()
        transition.validate()
        trust_state_sha256 = _sha256_json(trust_state.payload())
        unsigned = {
            "schema_version": _RECORD_SCHEMA,
            "authority_version": _AUTHORITY_SCHEMA,
            "generation": generation,
            "previous_record_sha256": previous_record_sha256,
            "trust_state": trust_state.payload(),
            "trust_state_sha256": trust_state_sha256,
            "transition": transition.payload(),
        }
        record = cls(
            generation=generation,
            previous_record_sha256=previous_record_sha256,
            trust_state=trust_state,
            trust_state_sha256=trust_state_sha256,
            transition=transition,
            record_sha256=_sha256_json(unsigned),
        )
        record.validate()
        return record


@dataclass(frozen=True)
class TrustStateAdvanceReceipt:
    previous_generation: int
    current_generation: int
    previous_record_sha256: str
    current_record_sha256: str
    previous_trust_state_sha256: str
    current_trust_state_sha256: str
    artifact_id: str
    artifact_sha256: str
    bundle_id: str
    bundle_sha256: str
    checkpoint_v4_id: str
    checkpoint_v4_sha256: str
    verified_at: datetime
    receipt_sha256: str
    schema_version: str = _RECEIPT_SCHEMA

    def unsigned_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "previous_generation": self.previous_generation,
            "current_generation": self.current_generation,
            "previous_record_sha256": self.previous_record_sha256,
            "current_record_sha256": self.current_record_sha256,
            "previous_trust_state_sha256": self.previous_trust_state_sha256,
            "current_trust_state_sha256": self.current_trust_state_sha256,
            "artifact_id": self.artifact_id,
            "artifact_sha256": self.artifact_sha256,
            "bundle_id": self.bundle_id,
            "bundle_sha256": self.bundle_sha256,
            "checkpoint_v4_id": self.checkpoint_v4_id,
            "checkpoint_v4_sha256": self.checkpoint_v4_sha256,
            "verified_at": _aware(self.verified_at, "verified_at").isoformat(),
        }

    def payload(self) -> dict[str, object]:
        expected = _sha256_json(self.unsigned_payload())
        if self.receipt_sha256 != expected:
            raise ValueError("TrustState receipt digest mismatch")
        return {**self.unsigned_payload(), "receipt_sha256": self.receipt_sha256}


class PersistentTrustStateAuthorityV1:
    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._history = directory / "history"
        self._current = directory / "current.json"
        self._lock = directory / ".authority.lock"

    def initialize(
        self,
        *,
        initial_state: QualificationTrustStateV4,
        transition: TrustStateTransitionContext,
    ) -> PersistentTrustStateRecord:
        self._ensure_layout()
        with self._exclusive_lock():
            if self._current.exists() or any(self._history.glob("*.json")):
                raise PersistentTrustStateAuthorityError(
                    "persistent TrustState authority is already initialized"
                )
            record = PersistentTrustStateRecord.build(
                generation=0,
                previous_record_sha256=_GENESIS,
                trust_state=initial_state,
                transition=transition,
            )
            self._write_history_record(record)
            self._write_current(record)
            return record

    def current(self) -> PersistentTrustStateRecord:
        self._ensure_layout()
        with self._exclusive_lock():
            return self._recover_and_load_current()

    def advance(
        self,
        *,
        next_state: QualificationTrustStateV4,
        transition: TrustStateTransitionContext,
        expected_generation: int,
        expected_record_sha256: str,
        expected_trust_state_sha256: str,
    ) -> tuple[PersistentTrustStateRecord, TrustStateAdvanceReceipt]:
        self._ensure_layout()
        with self._exclusive_lock():
            current = self._recover_and_load_current()
            if current.generation != expected_generation:
                raise PersistentTrustStateCASMismatch("TrustState generation CAS mismatch")
            if current.record_sha256 != expected_record_sha256:
                raise PersistentTrustStateCASMismatch("TrustState record SHA CAS mismatch")
            if current.trust_state_sha256 != expected_trust_state_sha256:
                raise PersistentTrustStateCASMismatch("TrustState payload SHA CAS mismatch")

            next_state.validate()
            transition.validate()
            if transition.checkpoint_v4_sha256 != next_state.checkpoint_v4_sha256:
                raise PersistentTrustStateAuthorityError(
                    "transition checkpoint SHA does not match next TrustState"
                )
            _validate_monotonic_transition(current.trust_state, next_state)
            if current.trust_state.payload() == next_state.payload():
                raise PersistentTrustStateAuthorityError(
                    "TrustState no-op advancement is not permitted"
                )

            record = PersistentTrustStateRecord.build(
                generation=current.generation + 1,
                previous_record_sha256=current.record_sha256,
                trust_state=next_state,
                transition=transition,
            )
            target = self._history_path(record.generation)
            if target.exists():
                existing = self._read_record(target)
                if existing.record_sha256 != record.record_sha256:
                    raise PersistentTrustStateCorruption(
                        "competing TrustState history record already exists"
                    )
                raise PersistentTrustStateAuthorityError(
                    "TrustState generation already committed"
                )

            self._write_history_record(record)
            self._write_current(record)
            return record, _receipt(previous=current, current=record)

    def export_history(self) -> tuple[PersistentTrustStateRecord, ...]:
        self._ensure_layout()
        with self._exclusive_lock():
            current = self._recover_and_load_current()
            records = self._validated_history_chain()
            if records[-1].record_sha256 != current.record_sha256:
                raise PersistentTrustStateCorruption(
                    "current TrustState record does not match validated history head"
                )
            return tuple(records)

    def _recover_and_load_current(self) -> PersistentTrustStateRecord:
        records = self._validated_history_chain()
        if not records:
            raise PersistentTrustStateAuthorityError(
                "persistent TrustState authority is not initialized"
            )

        if not self._current.exists():
            if len(records) == 1 and records[0].generation == 0:
                self._write_current(records[0])
                return records[0]
            raise PersistentTrustStateCorruption(
                "current TrustState pointer is missing with non-genesis history"
            )

        current = self._read_current()
        by_generation = {record.generation: record for record in records}
        canonical = by_generation.get(current.generation)
        if canonical is None or canonical.record_sha256 != current.record_sha256:
            raise PersistentTrustStateCorruption(
                "current TrustState pointer is not present in canonical history"
            )

        newer = [record for record in records if record.generation > current.generation]
        if not newer:
            return current
        if len(newer) == 1 and newer[0].generation == current.generation + 1:
            candidate = newer[0]
            if candidate.previous_record_sha256 != current.record_sha256:
                raise PersistentTrustStateCorruption(
                    "recoverable TrustState history successor has wrong parent"
                )
            self._write_current(candidate)
            return candidate
        raise PersistentTrustStateCorruption(
            "ambiguous TrustState rollback/fork detected"
        )

    def _validated_history_chain(self) -> list[PersistentTrustStateRecord]:
        paths = sorted(self._history.glob("*.json"))
        records = [self._read_record(path) for path in paths]
        if not records:
            return []
        expected_generation = 0
        previous_sha = _GENESIS
        for record in records:
            if record.generation != expected_generation:
                raise PersistentTrustStateCorruption(
                    "TrustState history generation gap or duplicate detected"
                )
            if record.previous_record_sha256 != previous_sha:
                raise PersistentTrustStateCorruption(
                    "TrustState history hash chain mismatch"
                )
            expected_generation += 1
            previous_sha = record.record_sha256
        return records

    def _read_current(self) -> PersistentTrustStateRecord:
        raw = _strict_json(self._current.read_bytes(), source="current TrustState")
        _exact_fields(
            raw,
            {"schema_version", "generation", "record_sha256"},
            "$",
        )
        if raw["schema_version"] != _CURRENT_SCHEMA:
            raise PersistentTrustStateCorruption("current TrustState schema mismatch")
        generation = _integer(raw["generation"], "$.generation")
        record_sha = _string(raw["record_sha256"], "$.record_sha256")
        record = self._read_record(self._history_path(generation))
        if record.record_sha256 != record_sha:
            raise PersistentTrustStateCorruption(
                "current TrustState record SHA pointer mismatch"
            )
        return record

    def _read_record(self, path: Path) -> PersistentTrustStateRecord:
        if not path.exists():
            raise PersistentTrustStateCorruption(
                f"missing TrustState history record: {path.name}"
            )
        raw = _strict_json(path.read_bytes(), source=f"history {path.name}")
        return _decode_record(raw)

    def _write_history_record(self, record: PersistentTrustStateRecord) -> None:
        record.validate()
        target = self._history_path(record.generation)
        if target.exists():
            raise PersistentTrustStateCorruption(
                "TrustState history target already exists"
            )
        _atomic_write_new(target, canonical_json_bytes(record.payload()))

    def _write_current(self, record: PersistentTrustStateRecord) -> None:
        record.validate()
        payload = canonical_json_bytes(
            {
                "schema_version": _CURRENT_SCHEMA,
                "generation": record.generation,
                "record_sha256": record.record_sha256,
            }
        )
        _atomic_replace(self._current, payload)

    def _history_path(self, generation: int) -> Path:
        return self._history / f"{generation:020d}.json"

    def _ensure_layout(self) -> None:
        self._history.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        self._directory.mkdir(parents=True, exist_ok=True)
        handle = self._lock.open("a+b")
        try:
            _lock_file(handle)
            yield
        finally:
            _unlock_file(handle)
            handle.close()


def _receipt(
    *,
    previous: PersistentTrustStateRecord,
    current: PersistentTrustStateRecord,
) -> TrustStateAdvanceReceipt:
    transition = current.transition
    unsigned = {
        "schema_version": _RECEIPT_SCHEMA,
        "previous_generation": previous.generation,
        "current_generation": current.generation,
        "previous_record_sha256": previous.record_sha256,
        "current_record_sha256": current.record_sha256,
        "previous_trust_state_sha256": previous.trust_state_sha256,
        "current_trust_state_sha256": current.trust_state_sha256,
        "artifact_id": transition.artifact_id,
        "artifact_sha256": transition.artifact_sha256,
        "bundle_id": transition.bundle_id,
        "bundle_sha256": transition.bundle_sha256,
        "checkpoint_v4_id": transition.checkpoint_v4_id,
        "checkpoint_v4_sha256": transition.checkpoint_v4_sha256,
        "verified_at": _aware(transition.verified_at, "verified_at").isoformat(),
    }
    receipt = TrustStateAdvanceReceipt(
        previous_generation=previous.generation,
        current_generation=current.generation,
        previous_record_sha256=previous.record_sha256,
        current_record_sha256=current.record_sha256,
        previous_trust_state_sha256=previous.trust_state_sha256,
        current_trust_state_sha256=current.trust_state_sha256,
        artifact_id=transition.artifact_id,
        artifact_sha256=transition.artifact_sha256,
        bundle_id=transition.bundle_id,
        bundle_sha256=transition.bundle_sha256,
        checkpoint_v4_id=transition.checkpoint_v4_id,
        checkpoint_v4_sha256=transition.checkpoint_v4_sha256,
        verified_at=transition.verified_at,
        receipt_sha256=_sha256_json(unsigned),
    )
    receipt.payload()
    return receipt


def _validate_monotonic_transition(
    previous: QualificationTrustStateV4,
    current: QualificationTrustStateV4,
) -> None:
    previous.validate()
    current.validate()
    if current.profile_event_count < previous.profile_event_count:
        raise PersistentTrustStateAuthorityError(
            "TrustState profile event count regression"
        )
    if (
        current.profile_event_count == previous.profile_event_count
        and current.profile_event_head_sha256 != previous.profile_event_head_sha256
    ):
        raise PersistentTrustStateAuthorityError(
            "TrustState profile head changed without event-count advance"
        )
    if current.transparency_tree_size < previous.transparency_tree_size:
        raise PersistentTrustStateAuthorityError(
            "TrustState transparency tree size regression"
        )
    if (
        current.transparency_tree_size == previous.transparency_tree_size
        and current.transparency_root_sha256 != previous.transparency_root_sha256
    ):
        raise PersistentTrustStateAuthorityError(
            "TrustState transparency root changed without tree-size advance"
        )


def _decode_record(raw: Mapping[str, object]) -> PersistentTrustStateRecord:
    _exact_fields(
        raw,
        {
            "schema_version",
            "authority_version",
            "generation",
            "previous_record_sha256",
            "trust_state",
            "trust_state_sha256",
            "transition",
            "record_sha256",
        },
        "$",
    )
    state_raw = _object(raw["trust_state"], "$.trust_state")
    _exact_fields(
        state_raw,
        {
            "profile_event_count",
            "profile_event_head_sha256",
            "transparency_tree_size",
            "transparency_root_sha256",
            "checkpoint_v4_sha256",
        },
        "$.trust_state",
    )
    state = QualificationTrustStateV4(
        profile_event_count=_integer(
            state_raw["profile_event_count"], "$.trust_state.profile_event_count"
        ),
        profile_event_head_sha256=_string(
            state_raw["profile_event_head_sha256"],
            "$.trust_state.profile_event_head_sha256",
        ),
        transparency_tree_size=_integer(
            state_raw["transparency_tree_size"],
            "$.trust_state.transparency_tree_size",
        ),
        transparency_root_sha256=_string(
            state_raw["transparency_root_sha256"],
            "$.trust_state.transparency_root_sha256",
        ),
        checkpoint_v4_sha256=_string(
            state_raw["checkpoint_v4_sha256"],
            "$.trust_state.checkpoint_v4_sha256",
        ),
    )
    transition_raw = _object(raw["transition"], "$.transition")
    _exact_fields(
        transition_raw,
        {
            "artifact_id",
            "artifact_sha256",
            "bundle_id",
            "bundle_sha256",
            "checkpoint_v4_id",
            "checkpoint_v4_sha256",
            "verified_at",
        },
        "$.transition",
    )
    transition = TrustStateTransitionContext(
        artifact_id=_string(transition_raw["artifact_id"], "$.transition.artifact_id"),
        artifact_sha256=_string(
            transition_raw["artifact_sha256"], "$.transition.artifact_sha256"
        ),
        bundle_id=_string(transition_raw["bundle_id"], "$.transition.bundle_id"),
        bundle_sha256=_string(
            transition_raw["bundle_sha256"], "$.transition.bundle_sha256"
        ),
        checkpoint_v4_id=_string(
            transition_raw["checkpoint_v4_id"], "$.transition.checkpoint_v4_id"
        ),
        checkpoint_v4_sha256=_string(
            transition_raw["checkpoint_v4_sha256"],
            "$.transition.checkpoint_v4_sha256",
        ),
        verified_at=_datetime(transition_raw["verified_at"], "$.transition.verified_at"),
    )
    record = PersistentTrustStateRecord(
        generation=_integer(raw["generation"], "$.generation"),
        previous_record_sha256=_string(
            raw["previous_record_sha256"], "$.previous_record_sha256"
        ),
        trust_state=state,
        trust_state_sha256=_string(
            raw["trust_state_sha256"], "$.trust_state_sha256"
        ),
        transition=transition,
        record_sha256=_string(raw["record_sha256"], "$.record_sha256"),
        schema_version=_string(raw["schema_version"], "$.schema_version"),
        authority_version=_string(raw["authority_version"], "$.authority_version"),
    )
    try:
        record.validate()
    except ValueError as exc:
        raise PersistentTrustStateCorruption(
            f"invalid persistent TrustState record: {exc}"
        ) from exc
    return record


def _strict_json(encoded: bytes, *, source: str) -> dict[str, object]:
    if not encoded:
        raise PersistentTrustStateCorruption(f"{source} is empty")
    if encoded.startswith(b"\xef\xbb\xbf"):
        raise PersistentTrustStateCorruption(f"{source} UTF-8 BOM is forbidden")
    try:
        text = encoded.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PersistentTrustStateCorruption(f"{source} is not valid UTF-8") from exc
    try:
        raw = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except PersistentTrustStateCorruption:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise PersistentTrustStateCorruption(f"{source} JSON is invalid") from exc
    if not isinstance(raw, dict):
        raise PersistentTrustStateCorruption(f"{source} root must be an object")
    if canonical_json_bytes(raw) != encoded:
        raise PersistentTrustStateCorruption(f"{source} is not canonical JSON")
    return raw


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PersistentTrustStateCorruption(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_float(value: str) -> NoReturn:
    raise PersistentTrustStateCorruption(f"floating-point JSON is forbidden: {value}")


def _reject_constant(value: str) -> NoReturn:
    raise PersistentTrustStateCorruption(f"non-finite JSON is forbidden: {value}")


def _exact_fields(
    value: Mapping[str, object],
    expected: set[str],
    path: str,
) -> None:
    actual = set(value)
    if actual != expected:
        raise PersistentTrustStateCorruption(
            f"{path} fields mismatch; missing={sorted(expected - actual)}; "
            f"unknown={sorted(actual - expected)}"
        )


def _object(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PersistentTrustStateCorruption(f"{path} must be an object")
    return value


def _string(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise PersistentTrustStateCorruption(f"{path} must be a string")
    return value


def _integer(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PersistentTrustStateCorruption(f"{path} must be an integer")
    return value


def _datetime(value: object, path: str) -> datetime:
    text = _string(value, path)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise PersistentTrustStateCorruption(
            f"{path} must be an ISO-8601 datetime"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PersistentTrustStateCorruption(f"{path} must be timezone-aware")
    return parsed.astimezone(UTC)


def _digest(value: str, name: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{name} must be a sha256 digest")
    return normalized


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _sha256_json(value: object) -> str:
    import hashlib

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _atomic_write_new(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _atomic_replace(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor: int | None = None
    temporary_path: str | None = None
    try:
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        directory_fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    except OSError:
        pass
    finally:
        os.close(directory_fd)


def _lock_file(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        if handle.read(1) == b"":
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_file(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
