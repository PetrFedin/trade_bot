from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import NoReturn

from app.qualification.portable_artifact_codec import canonical_json_bytes

_SCHEMA = "astra-verification-api-idempotency-record-v1"
_CURRENT_SCHEMA = "astra-verification-api-idempotency-current-v1"
_GENESIS = "0" * 64


class VerificationAPIIdempotencyError(RuntimeError):
    pass


class VerificationAPIIdempotencyConflict(VerificationAPIIdempotencyError):
    pass


class VerificationAPIIdempotencyCorruption(VerificationAPIIdempotencyError):
    pass


class VerificationAPIIdempotencyState(StrEnum):
    PREPARED = "PREPARED"
    AUTHORITY_COMMITTED = "AUTHORITY_COMMITTED"
    FINALIZED = "FINALIZED"


@dataclass(frozen=True)
class VerificationAPIAuthoritySnapshot:
    generation: int
    record_sha256: str
    trust_state_sha256: str

    def validate(self) -> None:
        if self.generation < 0:
            raise ValueError("authority generation must be non-negative")
        _digest(self.record_sha256, "authority record sha")
        _digest(self.trust_state_sha256, "authority trust state sha")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "generation": self.generation,
            "record_sha256": self.record_sha256,
            "trust_state_sha256": self.trust_state_sha256,
        }


@dataclass(frozen=True)
class VerificationAPIIdempotencyRecord:
    idempotency_key_sha256: str
    request_sha256: str
    authority_id: str
    trusted_root_set_id: str
    trusted_root_set_sha256: str
    state: VerificationAPIIdempotencyState
    generation: int
    previous_record_sha256: str
    authority_before: VerificationAPIAuthoritySnapshot
    artifact_id: str | None
    artifact_sha256: str | None
    bundle_id: str | None
    bundle_sha256: str | None
    checkpoint_v4_id: str | None
    checkpoint_v4_sha256: str | None
    observed_at: datetime
    authority_after: VerificationAPIAuthoritySnapshot | None = None
    transition_receipt: Mapping[str, object] | None = None
    response_payload: Mapping[str, object] | None = None
    response_sha256: str | None = None
    record_sha256: str = ""
    schema_version: str = _SCHEMA

    def validate(self) -> None:
        if self.schema_version != _SCHEMA:
            raise ValueError("idempotency record schema mismatch")
        if not self.authority_id.strip():
            raise ValueError("authority_id is required")
        if not self.trusted_root_set_id.strip():
            raise ValueError("trusted_root_set_id is required")
        _digest(self.trusted_root_set_sha256, "trusted_root_set_sha256")
        if self.generation < 0:
            raise ValueError("idempotency generation must be non-negative")
        _digest(self.idempotency_key_sha256, "idempotency_key_sha256")
        _digest(self.request_sha256, "request_sha256")
        _digest(self.previous_record_sha256, "previous_record_sha256")
        self.authority_before.validate()
        _aware(self.observed_at, "observed_at")

        for name, value in (
            ("artifact_sha256", self.artifact_sha256),
            ("bundle_sha256", self.bundle_sha256),
            ("checkpoint_v4_sha256", self.checkpoint_v4_sha256),
            ("response_sha256", self.response_sha256),
        ):
            if value is not None:
                _digest(value, name)

        if self.state is VerificationAPIIdempotencyState.PREPARED:
            if self.authority_after is not None:
                raise ValueError("PREPARED must not include authority_after")
            if self.transition_receipt is not None:
                raise ValueError("PREPARED must not include transition_receipt")
            if self.response_payload is not None or self.response_sha256 is not None:
                raise ValueError("PREPARED must not include response")
        elif self.state is VerificationAPIIdempotencyState.AUTHORITY_COMMITTED:
            if self.authority_after is None:
                raise ValueError("AUTHORITY_COMMITTED requires authority_after")
            self.authority_after.validate()
            if self.transition_receipt is None:
                raise ValueError("AUTHORITY_COMMITTED requires transition_receipt")
            if self.response_payload is not None or self.response_sha256 is not None:
                raise ValueError("AUTHORITY_COMMITTED must not include response")
        elif self.state is VerificationAPIIdempotencyState.FINALIZED:
            if self.authority_after is None:
                raise ValueError("FINALIZED requires authority_after")
            self.authority_after.validate()
            committed = self.authority_after != self.authority_before
            if committed and self.transition_receipt is None:
                raise ValueError(
                    "committed FINALIZED record requires transition_receipt"
                )
            if not committed and self.transition_receipt is not None:
                raise ValueError(
                    "non-committed FINALIZED record cannot carry transition_receipt"
                )
            if self.response_payload is None or self.response_sha256 is None:
                raise ValueError("FINALIZED requires canonical response")
            if _sha256(self.response_payload) != self.response_sha256:
                raise ValueError("FINALIZED response digest mismatch")

        if self.record_sha256:
            _digest(self.record_sha256, "record_sha256")
            if self.record_sha256 != _sha256(self.unsigned_payload()):
                raise ValueError("idempotency record digest mismatch")

    def unsigned_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "idempotency_key_sha256": self.idempotency_key_sha256,
            "request_sha256": self.request_sha256,
            "authority_id": self.authority_id,
            "trusted_root_set_id": self.trusted_root_set_id,
            "trusted_root_set_sha256": self.trusted_root_set_sha256,
            "state": self.state.value,
            "generation": self.generation,
            "previous_record_sha256": self.previous_record_sha256,
            "authority_before": self.authority_before.payload(),
            "artifact_id": self.artifact_id,
            "artifact_sha256": self.artifact_sha256,
            "bundle_id": self.bundle_id,
            "bundle_sha256": self.bundle_sha256,
            "checkpoint_v4_id": self.checkpoint_v4_id,
            "checkpoint_v4_sha256": self.checkpoint_v4_sha256,
            "observed_at": _aware(self.observed_at, "observed_at").isoformat(),
            "authority_after": (
                None if self.authority_after is None else self.authority_after.payload()
            ),
            "transition_receipt": (
                None if self.transition_receipt is None else dict(self.transition_receipt)
            ),
            "response_payload": (
                None if self.response_payload is None else dict(self.response_payload)
            ),
            "response_sha256": self.response_sha256,
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
        idempotency_key_sha256: str,
        request_sha256: str,
        authority_id: str,
        trusted_root_set_id: str,
        trusted_root_set_sha256: str,
        state: VerificationAPIIdempotencyState,
        generation: int,
        previous_record_sha256: str,
        authority_before: VerificationAPIAuthoritySnapshot,
        artifact_id: str | None,
        artifact_sha256: str | None,
        bundle_id: str | None,
        bundle_sha256: str | None,
        checkpoint_v4_id: str | None,
        checkpoint_v4_sha256: str | None,
        observed_at: datetime,
        authority_after: VerificationAPIAuthoritySnapshot | None = None,
        transition_receipt: Mapping[str, object] | None = None,
        response_payload: Mapping[str, object] | None = None,
        response_sha256: str | None = None,
    ) -> VerificationAPIIdempotencyRecord:
        provisional = cls(
            idempotency_key_sha256=idempotency_key_sha256,
            request_sha256=request_sha256,
            authority_id=authority_id,
            trusted_root_set_id=trusted_root_set_id,
            trusted_root_set_sha256=trusted_root_set_sha256,
            state=state,
            generation=generation,
            previous_record_sha256=previous_record_sha256,
            authority_before=authority_before,
            artifact_id=artifact_id,
            artifact_sha256=artifact_sha256,
            bundle_id=bundle_id,
            bundle_sha256=bundle_sha256,
            checkpoint_v4_id=checkpoint_v4_id,
            checkpoint_v4_sha256=checkpoint_v4_sha256,
            observed_at=observed_at,
            authority_after=authority_after,
            transition_receipt=transition_receipt,
            response_payload=response_payload,
            response_sha256=response_sha256,
        )
        provisional.validate()
        record = cls(
            **{
                **provisional.__dict__,
                "record_sha256": _sha256(provisional.unsigned_payload()),
            }
        )
        record.validate()
        return record


class VerificationAPIIdempotencyJournalV1:
    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def prepare(
        self,
        *,
        idempotency_key: str,
        request_sha256: str,
        authority_id: str,
        trusted_root_set_id: str,
        trusted_root_set_sha256: str,
        authority_before: VerificationAPIAuthoritySnapshot,
        artifact_id: str | None,
        artifact_sha256: str | None,
        bundle_id: str | None,
        bundle_sha256: str | None,
        checkpoint_v4_id: str | None,
        checkpoint_v4_sha256: str | None,
        observed_at: datetime,
    ) -> VerificationAPIIdempotencyRecord:
        key_sha = _key_sha(idempotency_key)
        _digest(request_sha256, "request_sha256")
        directory = self._key_directory(key_sha)
        directory.mkdir(parents=True, exist_ok=True)
        with self._exclusive_lock(key_sha):
            current = self._load_current_if_present(key_sha)
            if current is not None:
                if current.request_sha256 != request_sha256:
                    raise VerificationAPIIdempotencyConflict(
                        "idempotency key already bound to different request"
                    )
                return current
            record = VerificationAPIIdempotencyRecord.build(
                idempotency_key_sha256=key_sha,
                request_sha256=request_sha256,
                authority_id=authority_id,
                trusted_root_set_id=trusted_root_set_id,
                trusted_root_set_sha256=trusted_root_set_sha256,
                state=VerificationAPIIdempotencyState.PREPARED,
                generation=0,
                previous_record_sha256=_GENESIS,
                authority_before=authority_before,
                artifact_id=artifact_id,
                artifact_sha256=artifact_sha256,
                bundle_id=bundle_id,
                bundle_sha256=bundle_sha256,
                checkpoint_v4_id=checkpoint_v4_id,
                checkpoint_v4_sha256=checkpoint_v4_sha256,
                observed_at=observed_at,
            )
            self._append_record(key_sha, record)
            self._write_current(key_sha, record)
            return record

    def mark_authority_committed(
        self,
        *,
        idempotency_key: str,
        request_sha256: str,
        authority_after: VerificationAPIAuthoritySnapshot,
        transition_receipt: Mapping[str, object],
    ) -> VerificationAPIIdempotencyRecord:
        key_sha = _key_sha(idempotency_key)
        with self._exclusive_lock(key_sha):
            current = self._require_current(key_sha, request_sha256)
            if current.state is VerificationAPIIdempotencyState.FINALIZED:
                return current
            if current.state is VerificationAPIIdempotencyState.AUTHORITY_COMMITTED:
                if current.authority_after != authority_after:
                    raise VerificationAPIIdempotencyCorruption(
                        "authority commit replay does not match stored state"
                    )
                return current
            record = VerificationAPIIdempotencyRecord.build(
                idempotency_key_sha256=current.idempotency_key_sha256,
                request_sha256=current.request_sha256,
                authority_id=current.authority_id,
                trusted_root_set_id=current.trusted_root_set_id,
                trusted_root_set_sha256=current.trusted_root_set_sha256,
                state=VerificationAPIIdempotencyState.AUTHORITY_COMMITTED,
                generation=current.generation + 1,
                previous_record_sha256=current.record_sha256,
                authority_before=current.authority_before,
                artifact_id=current.artifact_id,
                artifact_sha256=current.artifact_sha256,
                bundle_id=current.bundle_id,
                bundle_sha256=current.bundle_sha256,
                checkpoint_v4_id=current.checkpoint_v4_id,
                checkpoint_v4_sha256=current.checkpoint_v4_sha256,
                observed_at=current.observed_at,
                authority_after=authority_after,
                transition_receipt=transition_receipt,
            )
            self._append_record(key_sha, record)
            self._write_current(key_sha, record)
            return record

    def finalize(
        self,
        *,
        idempotency_key: str,
        request_sha256: str,
        response_payload: Mapping[str, object],
    ) -> VerificationAPIIdempotencyRecord:
        key_sha = _key_sha(idempotency_key)
        with self._exclusive_lock(key_sha):
            current = self._require_current(key_sha, request_sha256)
            if current.state is VerificationAPIIdempotencyState.FINALIZED:
                if current.response_payload != response_payload:
                    raise VerificationAPIIdempotencyCorruption(
                        "finalized response replay mismatch"
                    )
                return current
            if current.state not in (
                VerificationAPIIdempotencyState.PREPARED,
                VerificationAPIIdempotencyState.AUTHORITY_COMMITTED,
            ):
                raise VerificationAPIIdempotencyError(
                    "idempotency record cannot be finalized from current state"
                )
            response = dict(response_payload)
            response_sha = _sha256(response)
            authority_after = (
                current.authority_before
                if current.state is VerificationAPIIdempotencyState.PREPARED
                else current.authority_after
            )
            transition_receipt = (
                None
                if current.state is VerificationAPIIdempotencyState.PREPARED
                else current.transition_receipt
            )
            record = VerificationAPIIdempotencyRecord.build(
                idempotency_key_sha256=current.idempotency_key_sha256,
                request_sha256=current.request_sha256,
                authority_id=current.authority_id,
                trusted_root_set_id=current.trusted_root_set_id,
                trusted_root_set_sha256=current.trusted_root_set_sha256,
                state=VerificationAPIIdempotencyState.FINALIZED,
                generation=current.generation + 1,
                previous_record_sha256=current.record_sha256,
                authority_before=current.authority_before,
                artifact_id=current.artifact_id,
                artifact_sha256=current.artifact_sha256,
                bundle_id=current.bundle_id,
                bundle_sha256=current.bundle_sha256,
                checkpoint_v4_id=current.checkpoint_v4_id,
                checkpoint_v4_sha256=current.checkpoint_v4_sha256,
                observed_at=current.observed_at,
                authority_after=authority_after,
                transition_receipt=transition_receipt,
                response_payload=response,
                response_sha256=response_sha,
            )
            self._append_record(key_sha, record)
            self._write_current(key_sha, record)
            return record

    def current(
        self,
        *,
        idempotency_key: str,
    ) -> VerificationAPIIdempotencyRecord | None:
        key_sha = _key_sha(idempotency_key)
        directory = self._key_directory(key_sha)
        if not directory.exists():
            return None
        with self._exclusive_lock(key_sha):
            return self._load_current_if_present(key_sha)

    def _require_current(
        self,
        key_sha: str,
        request_sha256: str,
    ) -> VerificationAPIIdempotencyRecord:
        current = self._load_current_if_present(key_sha)
        if current is None:
            raise VerificationAPIIdempotencyError("idempotency key is not prepared")
        if current.request_sha256 != request_sha256:
            raise VerificationAPIIdempotencyConflict(
                "idempotency key already bound to different request"
            )
        return current

    def _load_current_if_present(
        self,
        key_sha: str,
    ) -> VerificationAPIIdempotencyRecord | None:
        current_path = self._key_directory(key_sha) / "current.json"
        if not current_path.exists():
            return None
        current_raw = _strict_json(current_path.read_bytes(), source="idempotency current")
        _exact_fields(
            current_raw,
            {"schema_version", "generation", "record_sha256"},
            "$",
        )
        if current_raw["schema_version"] != _CURRENT_SCHEMA:
            raise VerificationAPIIdempotencyCorruption(
                "idempotency current schema mismatch"
            )
        generation = _integer(current_raw["generation"], "$.generation")
        record_sha = _string(current_raw["record_sha256"], "$.record_sha256")
        record = self._read_record(key_sha, generation)
        if record.record_sha256 != record_sha:
            raise VerificationAPIIdempotencyCorruption(
                "idempotency current record SHA mismatch"
            )
        return record

    def _append_record(
        self,
        key_sha: str,
        record: VerificationAPIIdempotencyRecord,
    ) -> None:
        record.validate()
        history = self._key_directory(key_sha) / "history"
        history.mkdir(parents=True, exist_ok=True)
        target = history / f"{record.generation:020d}.json"
        if target.exists():
            raise VerificationAPIIdempotencyCorruption(
                "idempotency generation already exists"
            )
        _atomic_write_new(target, canonical_json_bytes(record.payload()))

    def _write_current(
        self,
        key_sha: str,
        record: VerificationAPIIdempotencyRecord,
    ) -> None:
        payload = canonical_json_bytes(
            {
                "schema_version": _CURRENT_SCHEMA,
                "generation": record.generation,
                "record_sha256": record.record_sha256,
            }
        )
        _atomic_replace(self._key_directory(key_sha) / "current.json", payload)

    def _read_record(
        self,
        key_sha: str,
        generation: int,
    ) -> VerificationAPIIdempotencyRecord:
        path = (
            self._key_directory(key_sha)
            / "history"
            / f"{generation:020d}.json"
        )
        if not path.exists():
            raise VerificationAPIIdempotencyCorruption(
                "missing idempotency history record"
            )
        return _decode_record(_strict_json(path.read_bytes(), source=path.name))

    def _key_directory(self, key_sha: str) -> Path:
        return self._directory / key_sha

    @contextmanager
    def _exclusive_lock(self, key_sha: str) -> Iterator[None]:
        directory = self._key_directory(key_sha)
        directory.mkdir(parents=True, exist_ok=True)
        handle = (directory / ".lock").open("a+b")
        try:
            _lock_file(handle)
            yield
        finally:
            _unlock_file(handle)
            handle.close()


def _decode_record(raw: Mapping[str, object]) -> VerificationAPIIdempotencyRecord:
    _exact_fields(
        raw,
        {
            "schema_version",
            "idempotency_key_sha256",
            "request_sha256",
            "authority_id",
            "trusted_root_set_id",
            "trusted_root_set_sha256",
            "state",
            "generation",
            "previous_record_sha256",
            "authority_before",
            "artifact_id",
            "artifact_sha256",
            "bundle_id",
            "bundle_sha256",
            "checkpoint_v4_id",
            "checkpoint_v4_sha256",
            "observed_at",
            "authority_after",
            "transition_receipt",
            "response_payload",
            "response_sha256",
            "record_sha256",
        },
        "$",
    )
    before = _decode_snapshot(raw["authority_before"], "$.authority_before")
    after_raw = raw["authority_after"]
    after = None if after_raw is None else _decode_snapshot(after_raw, "$.authority_after")
    transition = raw["transition_receipt"]
    response = raw["response_payload"]
    if transition is not None and not isinstance(transition, Mapping):
        raise VerificationAPIIdempotencyCorruption(
            "$.transition_receipt must be an object or null"
        )
    if response is not None and not isinstance(response, Mapping):
        raise VerificationAPIIdempotencyCorruption(
            "$.response_payload must be an object or null"
        )
    try:
        state = VerificationAPIIdempotencyState(
            _string(raw["state"], "$.state")
        )
    except ValueError as exc:
        raise VerificationAPIIdempotencyCorruption(
            "unsupported idempotency state"
        ) from exc
    record = VerificationAPIIdempotencyRecord(
        idempotency_key_sha256=_string(
            raw["idempotency_key_sha256"], "$.idempotency_key_sha256"
        ),
        request_sha256=_string(raw["request_sha256"], "$.request_sha256"),
        authority_id=_string(raw["authority_id"], "$.authority_id"),
        trusted_root_set_id=_string(
            raw["trusted_root_set_id"],
            "$.trusted_root_set_id",
        ),
        trusted_root_set_sha256=_string(
            raw["trusted_root_set_sha256"],
            "$.trusted_root_set_sha256",
        ),
        state=state,
        generation=_integer(raw["generation"], "$.generation"),
        previous_record_sha256=_string(
            raw["previous_record_sha256"], "$.previous_record_sha256"
        ),
        authority_before=before,
        artifact_id=_optional_string(raw["artifact_id"], "$.artifact_id"),
        artifact_sha256=_optional_string(
            raw["artifact_sha256"], "$.artifact_sha256"
        ),
        bundle_id=_optional_string(raw["bundle_id"], "$.bundle_id"),
        bundle_sha256=_optional_string(raw["bundle_sha256"], "$.bundle_sha256"),
        checkpoint_v4_id=_optional_string(
            raw["checkpoint_v4_id"], "$.checkpoint_v4_id"
        ),
        checkpoint_v4_sha256=_optional_string(
            raw["checkpoint_v4_sha256"], "$.checkpoint_v4_sha256"
        ),
        observed_at=_datetime(raw["observed_at"], "$.observed_at"),
        authority_after=after,
        transition_receipt=None if transition is None else dict(transition),
        response_payload=None if response is None else dict(response),
        response_sha256=_optional_string(
            raw["response_sha256"], "$.response_sha256"
        ),
        record_sha256=_string(raw["record_sha256"], "$.record_sha256"),
        schema_version=_string(raw["schema_version"], "$.schema_version"),
    )
    try:
        record.validate()
    except ValueError as exc:
        raise VerificationAPIIdempotencyCorruption(
            f"invalid idempotency record: {exc}"
        ) from exc
    return record


def _decode_snapshot(
    value: object,
    path: str,
) -> VerificationAPIAuthoritySnapshot:
    if not isinstance(value, Mapping):
        raise VerificationAPIIdempotencyCorruption(f"{path} must be an object")
    _exact_fields(
        value,
        {"generation", "record_sha256", "trust_state_sha256"},
        path,
    )
    snapshot = VerificationAPIAuthoritySnapshot(
        generation=_integer(value["generation"], f"{path}.generation"),
        record_sha256=_string(
            value["record_sha256"], f"{path}.record_sha256"
        ),
        trust_state_sha256=_string(
            value["trust_state_sha256"], f"{path}.trust_state_sha256"
        ),
    )
    try:
        snapshot.validate()
    except ValueError as exc:
        raise VerificationAPIIdempotencyCorruption(
            f"invalid authority snapshot: {exc}"
        ) from exc
    return snapshot


def _key_sha(idempotency_key: str) -> str:
    key = idempotency_key.strip()
    if not key:
        raise VerificationAPIIdempotencyError("idempotency_key cannot be blank")
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


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


def _strict_json(encoded: bytes, *, source: str) -> dict[str, object]:
    if not encoded:
        raise VerificationAPIIdempotencyCorruption(f"{source} is empty")
    if encoded.startswith(b"\xef\xbb\xbf"):
        raise VerificationAPIIdempotencyCorruption(
            f"{source} UTF-8 BOM is forbidden"
        )
    try:
        text = encoded.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise VerificationAPIIdempotencyCorruption(
            f"{source} is not valid UTF-8"
        ) from exc
    try:
        raw = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except VerificationAPIIdempotencyCorruption:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise VerificationAPIIdempotencyCorruption(
            f"{source} JSON is invalid"
        ) from exc
    if not isinstance(raw, dict):
        raise VerificationAPIIdempotencyCorruption(
            f"{source} root must be an object"
        )
    if canonical_json_bytes(raw) != encoded:
        raise VerificationAPIIdempotencyCorruption(
            f"{source} is not canonical JSON"
        )
    return raw


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise VerificationAPIIdempotencyCorruption(
                f"duplicate JSON key: {key}"
            )
        result[key] = value
    return result


def _reject_float(value: str) -> NoReturn:
    raise VerificationAPIIdempotencyCorruption(
        f"floating-point JSON is forbidden: {value}"
    )


def _reject_constant(value: str) -> NoReturn:
    raise VerificationAPIIdempotencyCorruption(
        f"non-finite JSON is forbidden: {value}"
    )


def _exact_fields(
    value: Mapping[str, object],
    expected: set[str],
    path: str,
) -> None:
    actual = set(value)
    if actual != expected:
        raise VerificationAPIIdempotencyCorruption(
            f"{path} fields mismatch; missing={sorted(expected - actual)}; "
            f"unknown={sorted(actual - expected)}"
        )


def _string(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise VerificationAPIIdempotencyCorruption(f"{path} must be a string")
    return value


def _optional_string(value: object, path: str) -> str | None:
    if value is None:
        return None
    return _string(value, path)


def _integer(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise VerificationAPIIdempotencyCorruption(f"{path} must be an integer")
    return value


def _datetime(value: object, path: str) -> datetime:
    text = _string(value, path)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise VerificationAPIIdempotencyCorruption(
            f"{path} must be an ISO-8601 datetime"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise VerificationAPIIdempotencyCorruption(
            f"{path} must be timezone-aware"
        )
    return parsed.astimezone(UTC)


def _atomic_write_new(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
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
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


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
