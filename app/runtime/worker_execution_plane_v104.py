from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import hmac
import json
import os
from pathlib import Path
import platform
import threading
from typing import Any, Mapping, Protocol, Sequence

UTC = timezone.utc
PAPER_REST_BASE = "https://paper-api.alpaca.markets"
READ_ONLY_ENDPOINTS = frozenset({"account", "orders", "positions", "clock"})
ZERO_DIGEST = "0" * 64


class WorkerPlaneError(RuntimeError):
    pass


class IntegrityError(WorkerPlaneError):
    pass


class AuthorizationError(WorkerPlaneError):
    pass


class StaleClaimError(AuthorizationError):
    pass


class ReplayError(AuthorizationError):
    pass


class CapacityError(WorkerPlaneError):
    pass


class TransientTransportError(WorkerPlaneError):
    pass


class PermanentTransportError(WorkerPlaneError):
    pass


class WorkerState(str, Enum):
    IDLE = "IDLE"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    SPOOLING = "SPOOLING"
    UPLOADING = "UPLOADING"
    COMPLETED = "COMPLETED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    QUARANTINED = "QUARANTINED"


class ClaimOutcome(str, Enum):
    VERIFIED = "VERIFIED"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    QUARANTINED = "QUARANTINED"


class DlqReason(str, Enum):
    TRANSIENT_EXHAUSTED = "TRANSIENT_EXHAUSTED"
    PERMANENT_TRANSPORT = "PERMANENT_TRANSPORT"
    ATTESTATION_REJECTED = "ATTESTATION_REJECTED"
    CLAIM_REJECTED = "CLAIM_REJECTED"
    SPOOL_CAPACITY = "SPOOL_CAPACITY"
    EVIDENCE_CONFLICT = "EVIDENCE_CONFLICT"
    CRASH_RECOVERY = "CRASH_RECOVERY"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timezone-aware datetime required")
    return value.astimezone(UTC)


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with temp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _append_fsync(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


@dataclass(frozen=True, slots=True)
class WorkerPolicyV104:
    policy_id: str
    generation: int
    allowed_endpoints: tuple[str, ...] = ("account", "orders", "positions", "clock")
    claim_ttl: timedelta = timedelta(minutes=5)
    heartbeat_ttl: timedelta = timedelta(seconds=30)
    maximum_runtime: timedelta = timedelta(minutes=2)
    maximum_attempts: int = 3
    spool_max_files: int = 1000
    spool_max_bytes: int = 256 * 1024 * 1024
    multipart_part_bytes: int = 1024 * 1024
    evidence_retention: timedelta = timedelta(days=30)
    paper_only: bool = True
    mutations_allowed: bool = False
    external_order_routing_allowed: bool = False
    live_trading_allowed: bool = False

    def __post_init__(self) -> None:
        if not self.policy_id or self.generation <= 0:
            raise ValueError("invalid policy identity")
        if not self.allowed_endpoints or not set(self.allowed_endpoints) <= READ_ONLY_ENDPOINTS:
            raise ValueError("policy endpoints must be read-only")
        if self.claim_ttl <= timedelta(0) or self.heartbeat_ttl <= timedelta(0):
            raise ValueError("positive ttl required")
        if self.maximum_runtime <= timedelta(0) or self.maximum_runtime > timedelta(minutes=10):
            raise ValueError("maximum runtime out of bounds")
        if not 1 <= self.maximum_attempts <= 10:
            raise ValueError("maximum attempts out of bounds")
        if self.spool_max_files <= 0 or self.spool_max_bytes <= 0:
            raise ValueError("positive spool limits required")
        if self.multipart_part_bytes < 1024:
            raise ValueError("multipart part size too small")
        if not self.paper_only or self.mutations_allowed:
            raise ValueError("worker plane must remain paper-only and read-only")
        if self.external_order_routing_allowed or self.live_trading_allowed:
            raise ValueError("routing and live trading must remain disabled")

    @property
    def digest(self) -> str:
        payload = asdict(self)
        for key in ("claim_ttl", "heartbeat_ttl", "maximum_runtime", "evidence_retention"):
            payload[key] = int(payload[key].total_seconds())
        return _digest(payload)


@dataclass(frozen=True, slots=True)
class WorkerAttestationV104:
    worker_id: str
    deployment_id: str
    image_digest: str
    source_commit: str
    policy_digest: str
    generation: int
    created_at: datetime
    expires_at: datetime
    nonce: str
    key_id: str
    signature: str = ""

    def payload(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "deployment_id": self.deployment_id,
            "image_digest": self.image_digest,
            "source_commit": self.source_commit,
            "policy_digest": self.policy_digest,
            "generation": self.generation,
            "created_at": _utc(self.created_at).isoformat(),
            "expires_at": _utc(self.expires_at).isoformat(),
            "nonce": self.nonce,
            "key_id": self.key_id,
        }


@dataclass(frozen=True, slots=True)
class SignedWorkClaimV104:
    claim_id: str
    campaign_id: str
    run_id: str
    generation: int
    fencing_token: int
    endpoints: tuple[str, ...]
    issued_at: datetime
    not_before: datetime
    expires_at: datetime
    worker_id: str
    deployment_id: str
    policy_digest: str
    nonce: str
    key_id: str
    signature: str = ""

    def payload(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "campaign_id": self.campaign_id,
            "run_id": self.run_id,
            "generation": self.generation,
            "fencing_token": self.fencing_token,
            "endpoints": list(self.endpoints),
            "issued_at": _utc(self.issued_at).isoformat(),
            "not_before": _utc(self.not_before).isoformat(),
            "expires_at": _utc(self.expires_at).isoformat(),
            "worker_id": self.worker_id,
            "deployment_id": self.deployment_id,
            "policy_digest": self.policy_digest,
            "nonce": self.nonce,
            "key_id": self.key_id,
        }


class HmacKeyRingV104:
    def __init__(self, keys: Mapping[str, bytes]) -> None:
        if not keys or any(not key_id or len(secret) < 32 for key_id, secret in keys.items()):
            raise ValueError("each HMAC key must be at least 32 bytes")
        self._keys = dict(keys)

    def sign_attestation(self, attestation: WorkerAttestationV104) -> WorkerAttestationV104:
        secret = self._secret(attestation.key_id)
        signature = hmac.new(secret, _canonical(attestation.payload()), hashlib.sha256).hexdigest()
        return replace(attestation, signature=signature)

    def verify_attestation(self, attestation: WorkerAttestationV104) -> None:
        expected = hmac.new(self._secret(attestation.key_id), _canonical(attestation.payload()), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, attestation.signature):
            raise AuthorizationError("attestation signature mismatch")

    def sign_claim(self, claim: SignedWorkClaimV104) -> SignedWorkClaimV104:
        signature = hmac.new(self._secret(claim.key_id), _canonical(claim.payload()), hashlib.sha256).hexdigest()
        return replace(claim, signature=signature)

    def verify_claim(self, claim: SignedWorkClaimV104) -> None:
        expected = hmac.new(self._secret(claim.key_id), _canonical(claim.payload()), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, claim.signature):
            raise AuthorizationError("claim signature mismatch")

    def _secret(self, key_id: str) -> bytes:
        try:
            return self._keys[key_id]
        except KeyError as exc:
            raise AuthorizationError("unknown signing key") from exc


class ReplayLedgerV104:
    def __init__(self) -> None:
        self._claims: set[str] = set()
        self._nonces: set[str] = set()
        self._lock = threading.Lock()

    def consume(self, claim_id: str, nonce: str) -> None:
        with self._lock:
            if claim_id in self._claims or nonce in self._nonces:
                raise ReplayError("claim or nonce already consumed")
            self._claims.add(claim_id)
            self._nonces.add(nonce)


@dataclass(frozen=True, slots=True)
class WorkerHeartbeatV104:
    worker_id: str
    claim_id: str
    generation: int
    fencing_token: int
    sequence: int
    observed_at: datetime


class HeartbeatGuardV104:
    def __init__(self) -> None:
        self._latest: WorkerHeartbeatV104 | None = None

    def accept(self, heartbeat: WorkerHeartbeatV104, now: datetime, ttl: timedelta) -> None:
        now = _utc(now)
        observed_at = _utc(heartbeat.observed_at)
        if observed_at > now + timedelta(seconds=1):
            raise StaleClaimError("heartbeat from future")
        if now - observed_at > ttl:
            raise StaleClaimError("heartbeat stale")
        if self._latest is not None:
            if heartbeat.worker_id != self._latest.worker_id or heartbeat.claim_id != self._latest.claim_id:
                raise StaleClaimError("heartbeat identity changed")
            if heartbeat.generation != self._latest.generation or heartbeat.fencing_token != self._latest.fencing_token:
                raise StaleClaimError("heartbeat fence changed")
            if heartbeat.sequence <= self._latest.sequence:
                raise StaleClaimError("heartbeat sequence regression")
            if observed_at < _utc(self._latest.observed_at):
                raise StaleClaimError("heartbeat time regression")
        self._latest = heartbeat


class ReadOnlyTransportV104(Protocol):
    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
        tls_verify: bool,
        allow_redirects: bool,
    ) -> Mapping[str, Any]: ...


class ReadOnlyAlpacaRunnerV104:
    _PATHS = {
        "account": "/v2/account",
        "orders": "/v2/orders?status=open&direction=asc",
        "positions": "/v2/positions",
        "clock": "/v2/clock",
    }

    def __init__(
        self,
        transport: ReadOnlyTransportV104,
        key_id: str,
        secret: str,
        timeout_seconds: float = 5.0,
        maximum_response_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        if not key_id or not secret:
            raise ValueError("paper credentials required")
        if not 0.1 <= timeout_seconds <= 30.0:
            raise ValueError("timeout out of bounds")
        if maximum_response_bytes <= 0 or maximum_response_bytes > 16 * 1024 * 1024:
            raise ValueError("maximum response size out of bounds")
        self._transport = transport
        self._key_id = key_id
        self._secret = secret
        self._timeout = timeout_seconds
        self._maximum_response_bytes = maximum_response_bytes

    def probe(self, endpoints: Sequence[str]) -> dict[str, Mapping[str, Any]]:
        unique = tuple(dict.fromkeys(endpoints))
        if not unique or not set(unique) <= READ_ONLY_ENDPOINTS:
            raise AuthorizationError("read-only endpoint allowlist violation")
        headers = {"APCA-API-KEY-ID": self._key_id, "APCA-API-SECRET-KEY": self._secret}
        results: dict[str, Mapping[str, Any]] = {}
        for endpoint in unique:
            url = PAPER_REST_BASE + self._PATHS[endpoint]
            response = self._transport.get(
                url,
                headers=headers,
                timeout_seconds=self._timeout,
                tls_verify=True,
                allow_redirects=False,
            )
            if not isinstance(response, Mapping):
                raise PermanentTransportError("paper response must be an object")
            sanitized = _sanitize(response)
            if len(_canonical(sanitized)) > self._maximum_response_bytes:
                raise PermanentTransportError("paper response exceeds size limit")
            results[endpoint] = sanitized
        return results

    def submit(self, *_: Any, **__: Any) -> None:
        raise AuthorizationError("broker mutations are prohibited in Schema 104")

    replace = submit
    cancel = submit


def _sanitize(value: Any) -> Any:
    secret_tokens = {"secret", "token", "authorization", "api_key", "apca-api-secret-key"}
    if isinstance(value, Mapping):
        return {str(key): "[REDACTED]" if str(key).lower() in secret_tokens else _sanitize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class JournalEventV104:
    sequence: int
    event_type: str
    state: WorkerState
    occurred_at: datetime
    attributes: Mapping[str, Any]
    previous_digest: str
    event_digest: str


class WorkerEventJournalV104:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def append(self, event_type: str, state: WorkerState, occurred_at: datetime, attributes: Mapping[str, Any]) -> JournalEventV104:
        with self._lock:
            events = self.verify()
            sequence = len(events) + 1
            previous = events[-1].event_digest if events else ZERO_DIGEST
            payload = {
                "sequence": sequence,
                "event_type": event_type,
                "state": state.value,
                "occurred_at": _utc(occurred_at).isoformat(),
                "attributes": _sanitize(dict(attributes)),
                "previous_digest": previous,
            }
            event_digest = _digest(payload)
            record = {**payload, "event_digest": event_digest}
            _append_fsync(self.path, _canonical(record) + b"\n")
            return JournalEventV104(sequence, event_type, state, _utc(occurred_at), record["attributes"], previous, event_digest)

    def verify(self) -> tuple[JournalEventV104, ...]:
        if not self.path.exists():
            return ()
        previous = ZERO_DIGEST
        events: list[JournalEventV104] = []
        with self.path.open("rb") as handle:
            for expected_sequence, raw in enumerate(handle, start=1):
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise IntegrityError("journal JSON corruption") from exc
                if record.get("sequence") != expected_sequence or record.get("previous_digest") != previous:
                    raise IntegrityError("journal sequence or chain mismatch")
                payload = {key: record[key] for key in ("sequence", "event_type", "state", "occurred_at", "attributes", "previous_digest")}
                if _digest(payload) != record.get("event_digest"):
                    raise IntegrityError("journal digest mismatch")
                event = JournalEventV104(
                    sequence=record["sequence"],
                    event_type=record["event_type"],
                    state=WorkerState(record["state"]),
                    occurred_at=datetime.fromisoformat(record["occurred_at"]),
                    attributes=record["attributes"],
                    previous_digest=record["previous_digest"],
                    event_digest=record["event_digest"],
                )
                events.append(event)
                previous = event.event_digest
        return tuple(events)


@dataclass(frozen=True, slots=True)
class SpoolRecordV104:
    record_id: str
    claim_id: str
    payload_path: str
    byte_length: int
    payload_digest: str
    created_at: datetime
    retention_until: datetime
    previous_manifest_digest: str
    manifest_digest: str
    acknowledged: bool = False


class EvidenceSpoolV104:
    def __init__(self, root: Path, max_files: int, max_bytes: int) -> None:
        self.root = root
        self.payload_dir = root / "payloads"
        self.manifest_path = root / "manifest.jsonl"
        self.max_files = max_files
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def enqueue(self, record_id: str, claim_id: str, payload: bytes, created_at: datetime, retention: timedelta) -> SpoolRecordV104:
        with self._lock:
            records = list(self.verify())
            existing = next((record for record in records if record.record_id == record_id), None)
            digest = hashlib.sha256(payload).hexdigest()
            if existing:
                if existing.payload_digest != digest or existing.byte_length != len(payload):
                    raise IntegrityError("spool replay conflict")
                return existing
            active = [record for record in records if not record.acknowledged]
            if len(active) >= self.max_files or sum(record.byte_length for record in active) + len(payload) > self.max_bytes:
                raise CapacityError("spool capacity exceeded")
            payload_path = self.payload_dir / f"{record_id}.bin"
            _atomic_write(payload_path, payload)
            previous = records[-1].manifest_digest if records else ZERO_DIGEST
            base = {
                "record_id": record_id,
                "claim_id": claim_id,
                "payload_path": str(payload_path.relative_to(self.root)),
                "byte_length": len(payload),
                "payload_digest": digest,
                "created_at": _utc(created_at).isoformat(),
                "retention_until": (_utc(created_at) + retention).isoformat(),
                "previous_manifest_digest": previous,
                "acknowledged": False,
            }
            manifest_digest = _digest(base)
            _append_fsync(self.manifest_path, _canonical({**base, "manifest_digest": manifest_digest}) + b"\n")
            return SpoolRecordV104(
                record_id, claim_id, base["payload_path"], len(payload), digest,
                _utc(created_at), _utc(created_at) + retention, previous, manifest_digest, False,
            )

    def acknowledge(self, record_id: str, at: datetime) -> SpoolRecordV104:
        with self._lock:
            records = list(self.verify())
            target = next((record for record in records if record.record_id == record_id), None)
            if target is None:
                raise KeyError(record_id)
            if target.acknowledged:
                return target
            previous = records[-1].manifest_digest if records else ZERO_DIGEST
            base = {
                "record_id": target.record_id,
                "claim_id": target.claim_id,
                "payload_path": target.payload_path,
                "byte_length": target.byte_length,
                "payload_digest": target.payload_digest,
                "created_at": _utc(target.created_at).isoformat(),
                "retention_until": _utc(target.retention_until).isoformat(),
                "previous_manifest_digest": previous,
                "acknowledged": True,
                "acknowledged_at": _utc(at).isoformat(),
            }
            digest = _digest(base)
            _append_fsync(self.manifest_path, _canonical({**base, "manifest_digest": digest}) + b"\n")
            return replace(target, previous_manifest_digest=previous, manifest_digest=digest, acknowledged=True)

    def verify(self) -> tuple[SpoolRecordV104, ...]:
        if not self.manifest_path.exists():
            return ()
        previous = ZERO_DIGEST
        latest: dict[str, SpoolRecordV104] = {}
        order: list[str] = []
        with self.manifest_path.open("rb") as handle:
            for raw in handle:
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise IntegrityError("spool manifest JSON corruption") from exc
                if record.get("previous_manifest_digest") != previous:
                    raise IntegrityError("spool manifest chain mismatch")
                payload = {key: record[key] for key in record if key != "manifest_digest"}
                digest = _digest(payload)
                if digest != record.get("manifest_digest"):
                    raise IntegrityError("spool manifest digest mismatch")
                payload_path = self.root / record["payload_path"]
                if not payload_path.is_file():
                    raise IntegrityError("spool payload missing")
                data = payload_path.read_bytes()
                if len(data) != record["byte_length"] or hashlib.sha256(data).hexdigest() != record["payload_digest"]:
                    raise IntegrityError("spool payload mismatch")
                item = SpoolRecordV104(
                    record_id=record["record_id"],
                    claim_id=record["claim_id"],
                    payload_path=record["payload_path"],
                    byte_length=record["byte_length"],
                    payload_digest=record["payload_digest"],
                    created_at=datetime.fromisoformat(record["created_at"]),
                    retention_until=datetime.fromisoformat(record["retention_until"]),
                    previous_manifest_digest=record["previous_manifest_digest"],
                    manifest_digest=record["manifest_digest"],
                    acknowledged=record.get("acknowledged", False),
                )
                if item.record_id not in latest:
                    order.append(item.record_id)
                latest[item.record_id] = item
                previous = item.manifest_digest
        return tuple(latest[record_id] for record_id in order)

    def pending(self) -> tuple[SpoolRecordV104, ...]:
        return tuple(record for record in self.verify() if not record.acknowledged)


class ObjectStoreV104(Protocol):
    def create_upload(self, object_key: str, metadata: Mapping[str, str]) -> str: ...
    def upload_part(self, upload_id: str, part_number: int, data: bytes, digest: str) -> str: ...
    def complete_upload(self, upload_id: str, parts: Sequence[tuple[int, str]], total_digest: str) -> str: ...
    def abort_upload(self, upload_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class UploadCheckpointV104:
    record_id: str
    upload_id: str
    object_key: str
    payload_digest: str
    byte_length: int
    completed_parts: tuple[tuple[int, str], ...] = ()
    completed_object_digest: str | None = None


class ResumableUploaderV104:
    def __init__(self, root: Path, store: ObjectStoreV104, part_bytes: int, checkpoint_key: bytes) -> None:
        if part_bytes <= 0:
            raise ValueError("positive part size required")
        if len(checkpoint_key) < 32:
            raise ValueError("checkpoint HMAC key must be at least 32 bytes")
        self.root = root
        self.store = store
        self.part_bytes = part_bytes
        self._checkpoint_key = checkpoint_key
        self.root.mkdir(parents=True, exist_ok=True)

    def upload(self, record: SpoolRecordV104, spool_root: Path) -> UploadCheckpointV104:
        checkpoint_path = self.root / f"{record.record_id}.json"
        if checkpoint_path.exists():
            checkpoint = self._read_checkpoint(checkpoint_path)
            if checkpoint.payload_digest != record.payload_digest or checkpoint.byte_length != record.byte_length:
                raise IntegrityError("upload checkpoint conflicts with spool record")
        else:
            object_key = f"evidence/{record.claim_id}/{record.record_id}.json"
            upload_id = self.store.create_upload(object_key, {"sha256": record.payload_digest, "claim_id": record.claim_id})
            checkpoint = UploadCheckpointV104(record.record_id, upload_id, object_key, record.payload_digest, record.byte_length)
            self._write_checkpoint(checkpoint_path, checkpoint)
        if checkpoint.completed_object_digest:
            return checkpoint
        payload = (spool_root / record.payload_path).read_bytes()
        completed = dict(checkpoint.completed_parts)
        for index in range(0, len(payload), self.part_bytes):
            part_number = index // self.part_bytes + 1
            if part_number in completed:
                continue
            part = payload[index:index + self.part_bytes]
            part_digest = hashlib.sha256(part).hexdigest()
            etag = self.store.upload_part(checkpoint.upload_id, part_number, part, part_digest)
            if not etag:
                raise TransientTransportError("object store returned empty etag")
            completed[part_number] = etag
            checkpoint = replace(checkpoint, completed_parts=tuple(sorted(completed.items())))
            self._write_checkpoint(checkpoint_path, checkpoint)
        object_digest = self.store.complete_upload(checkpoint.upload_id, checkpoint.completed_parts, record.payload_digest)
        if object_digest != record.payload_digest:
            raise IntegrityError("completed object digest mismatch")
        checkpoint = replace(checkpoint, completed_object_digest=object_digest)
        self._write_checkpoint(checkpoint_path, checkpoint)
        return checkpoint

    def _write_checkpoint(self, path: Path, checkpoint: UploadCheckpointV104) -> None:
        payload = asdict(checkpoint)
        signature = hmac.new(self._checkpoint_key, _canonical(payload), hashlib.sha256).hexdigest()
        _atomic_write(path, _canonical({"payload": payload, "signature": signature}))

    def _read_checkpoint(self, path: Path) -> UploadCheckpointV104:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        payload = envelope.get("payload")
        signature = envelope.get("signature", "")
        if not isinstance(payload, dict):
            raise IntegrityError("upload checkpoint payload missing")
        expected = hmac.new(self._checkpoint_key, _canonical(payload), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise IntegrityError("upload checkpoint signature mismatch")
        payload["completed_parts"] = tuple(tuple(item) for item in payload.get("completed_parts", []))
        return UploadCheckpointV104(**payload)


@dataclass(frozen=True, slots=True)
class DlqRecordV104:
    sequence: int
    record_id: str
    claim_id: str
    reason: DlqReason
    attempt: int
    occurred_at: datetime
    detail: str
    previous_digest: str
    record_digest: str
    released: bool = False


class DeadLetterQueueV104:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def enqueue(self, record_id: str, claim_id: str, reason: DlqReason, attempt: int, occurred_at: datetime, detail: str) -> DlqRecordV104:
        with self._lock:
            records = self.verify()
            existing = next((item for item in records if item.record_id == record_id and not item.released), None)
            if existing:
                return existing
            previous = records[-1].record_digest if records else ZERO_DIGEST
            payload = {
                "sequence": len(records) + 1,
                "record_id": record_id,
                "claim_id": claim_id,
                "reason": reason.value,
                "attempt": attempt,
                "occurred_at": _utc(occurred_at).isoformat(),
                "detail": detail[:500],
                "previous_digest": previous,
                "released": False,
            }
            digest = _digest(payload)
            _append_fsync(self.path, _canonical({**payload, "record_digest": digest}) + b"\n")
            return DlqRecordV104(payload["sequence"], record_id, claim_id, reason, attempt, _utc(occurred_at), payload["detail"], previous, digest)

    def release(self, record_id: str, operator: str, at: datetime) -> DlqRecordV104:
        if not operator:
            raise AuthorizationError("operator identity required")
        with self._lock:
            records = self.verify()
            target = next((item for item in reversed(records) if item.record_id == record_id and not item.released), None)
            if target is None:
                raise KeyError(record_id)
            previous = records[-1].record_digest if records else ZERO_DIGEST
            payload = {
                "sequence": len(records) + 1,
                "record_id": target.record_id,
                "claim_id": target.claim_id,
                "reason": target.reason.value,
                "attempt": target.attempt,
                "occurred_at": _utc(at).isoformat(),
                "detail": f"released_by:{operator}",
                "previous_digest": previous,
                "released": True,
            }
            digest = _digest(payload)
            _append_fsync(self.path, _canonical({**payload, "record_digest": digest}) + b"\n")
            return DlqRecordV104(payload["sequence"], target.record_id, target.claim_id, target.reason, target.attempt, _utc(at), payload["detail"], previous, digest, True)

    def verify(self) -> tuple[DlqRecordV104, ...]:
        if not self.path.exists():
            return ()
        previous = ZERO_DIGEST
        latest: dict[str, DlqRecordV104] = {}
        order: list[str] = []
        with self.path.open("rb") as handle:
            for expected, raw in enumerate(handle, start=1):
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise IntegrityError("dlq JSON corruption") from exc
                if record.get("sequence") != expected or record.get("previous_digest") != previous:
                    raise IntegrityError("dlq chain mismatch")
                payload = {key: record[key] for key in record if key != "record_digest"}
                if _digest(payload) != record.get("record_digest"):
                    raise IntegrityError("dlq digest mismatch")
                item = DlqRecordV104(
                    sequence=record["sequence"], record_id=record["record_id"], claim_id=record["claim_id"],
                    reason=DlqReason(record["reason"]), attempt=record["attempt"], occurred_at=datetime.fromisoformat(record["occurred_at"]),
                    detail=record["detail"], previous_digest=record["previous_digest"], record_digest=record["record_digest"], released=record.get("released", False),
                )
                if item.record_id not in latest:
                    order.append(item.record_id)
                latest[item.record_id] = item
                previous = item.record_digest
        return tuple(latest[record_id] for record_id in order)

    def pending(self) -> tuple[DlqRecordV104, ...]:
        return tuple(item for item in self.verify() if not item.released)


@dataclass(frozen=True, slots=True)
class ExecutionResultV104:
    claim_id: str
    outcome: ClaimOutcome
    state: WorkerState
    evidence_record_id: str | None
    evidence_digest: str | None
    attempt: int
    detail: str
    completed_at: datetime


class WorkerExecutionPlaneV104:
    def __init__(
        self,
        policy: WorkerPolicyV104,
        keyring: HmacKeyRingV104,
        replay_ledger: ReplayLedgerV104,
        runner: ReadOnlyAlpacaRunnerV104,
        spool: EvidenceSpoolV104,
        uploader: ResumableUploaderV104,
        dlq: DeadLetterQueueV104,
        journal: WorkerEventJournalV104,
    ) -> None:
        self.policy = policy
        self.keyring = keyring
        self.replay_ledger = replay_ledger
        self.runner = runner
        self.spool = spool
        self.uploader = uploader
        self.dlq = dlq
        self.journal = journal
        self.state = WorkerState.IDLE
        self._heartbeat = HeartbeatGuardV104()
        self._active_claim: SignedWorkClaimV104 | None = None
        self._lock = threading.Lock()

    def execute(self, claim: SignedWorkClaimV104, attestation: WorkerAttestationV104, now: datetime) -> ExecutionResultV104:
        now = _utc(now)
        with self._lock:
            if self.state not in {WorkerState.IDLE, WorkerState.COMPLETED}:
                raise WorkerPlaneError("worker already active")
            try:
                self._authorize(claim, attestation, now)
                self.replay_ledger.consume(claim.claim_id, claim.nonce)
            except AuthorizationError as exc:
                self.state = WorkerState.QUARANTINED
                self.journal.append("CLAIM_REJECTED", self.state, now, {"claim_id": claim.claim_id, "reason": str(exc)})
                self.dlq.enqueue(f"dlq-{claim.claim_id}", claim.claim_id, DlqReason.CLAIM_REJECTED, 0, now, str(exc))
                raise
            self._active_claim = claim
            self.state = WorkerState.CLAIMED
            self.journal.append("CLAIM_ACCEPTED", self.state, now, {"claim_id": claim.claim_id, "fencing_token": claim.fencing_token})

        started = now
        for attempt in range(1, self.policy.maximum_attempts + 1):
            try:
                if now - started > self.policy.maximum_runtime:
                    raise StaleClaimError("maximum runtime exceeded")
                self.state = WorkerState.RUNNING
                self.journal.append("PROBE_STARTED", self.state, now, {"claim_id": claim.claim_id, "attempt": attempt})
                probe = self.runner.probe(claim.endpoints)
                evidence = {
                    "schema": 104,
                    "claim": claim.payload(),
                    "attestation": attestation.payload(),
                    "worker_runtime": {
                        "python": platform.python_version(),
                        "platform": platform.system(),
                    },
                    "probe": probe,
                    "paper_only": True,
                    "mutations_allowed": False,
                    "external_order_routing_allowed": False,
                    "live_trading_allowed": False,
                    "completed_at": now.isoformat(),
                }
                encoded = _canonical(evidence)
                record_id = f"evidence-{claim.claim_id}"
                self.state = WorkerState.SPOOLING
                record = self.spool.enqueue(record_id, claim.claim_id, encoded, now, self.policy.evidence_retention)
                self.journal.append("EVIDENCE_SPOOLED", self.state, now, {"record_id": record.record_id, "digest": record.payload_digest})
                self.state = WorkerState.UPLOADING
                checkpoint = self.uploader.upload(record, self.spool.root)
                self.spool.acknowledge(record.record_id, now)
                self.state = WorkerState.COMPLETED
                self.journal.append("CLAIM_COMPLETED", self.state, now, {"claim_id": claim.claim_id, "object_digest": checkpoint.completed_object_digest})
                self._active_claim = None
                return ExecutionResultV104(claim.claim_id, ClaimOutcome.VERIFIED, self.state, record.record_id, record.payload_digest, attempt, "verified", now)
            except TransientTransportError as exc:
                self.journal.append("TRANSIENT_FAILURE", self.state, now, {"claim_id": claim.claim_id, "attempt": attempt, "reason": str(exc)})
                if attempt >= self.policy.maximum_attempts:
                    self.state = WorkerState.RECOVERY_REQUIRED
                    self.dlq.enqueue(f"dlq-{claim.claim_id}", claim.claim_id, DlqReason.TRANSIENT_EXHAUSTED, attempt, now, str(exc))
                    self.journal.append("CLAIM_RECOVERY_REQUIRED", self.state, now, {"claim_id": claim.claim_id})
                    self._active_claim = None
                    return ExecutionResultV104(claim.claim_id, ClaimOutcome.RECOVERY_REQUIRED, self.state, None, None, attempt, str(exc), now)
            except (PermanentTransportError, CapacityError, IntegrityError, StaleClaimError) as exc:
                self.state = WorkerState.QUARANTINED if isinstance(exc, IntegrityError) else WorkerState.RECOVERY_REQUIRED
                reason = DlqReason.EVIDENCE_CONFLICT if isinstance(exc, IntegrityError) else DlqReason.PERMANENT_TRANSPORT
                if isinstance(exc, CapacityError):
                    reason = DlqReason.SPOOL_CAPACITY
                self.dlq.enqueue(f"dlq-{claim.claim_id}", claim.claim_id, reason, attempt, now, str(exc))
                self.journal.append("CLAIM_FAILED", self.state, now, {"claim_id": claim.claim_id, "reason": str(exc)})
                self._active_claim = None
                return ExecutionResultV104(claim.claim_id, ClaimOutcome.QUARANTINED if self.state is WorkerState.QUARANTINED else ClaimOutcome.RECOVERY_REQUIRED, self.state, None, None, attempt, str(exc), now)
        raise AssertionError("unreachable")

    def heartbeat(self, heartbeat: WorkerHeartbeatV104, now: datetime) -> None:
        claim = self._active_claim
        if claim is None:
            raise StaleClaimError("no active claim")
        if heartbeat.worker_id != claim.worker_id or heartbeat.claim_id != claim.claim_id:
            raise StaleClaimError("heartbeat does not match active claim")
        if heartbeat.generation != claim.generation or heartbeat.fencing_token != claim.fencing_token:
            raise StaleClaimError("heartbeat fence mismatch")
        self._heartbeat.accept(heartbeat, now, self.policy.heartbeat_ttl)
        self.journal.append("WORKER_HEARTBEAT", self.state, now, {"claim_id": heartbeat.claim_id, "sequence": heartbeat.sequence})

    def recover_after_crash(self, now: datetime) -> WorkerState:
        events = self.journal.verify()
        if not events:
            self.state = WorkerState.IDLE
            return self.state
        terminal = {"CLAIM_COMPLETED", "CLAIM_FAILED", "CLAIM_RECOVERY_REQUIRED", "CLAIM_REJECTED", "CRASH_RECOVERY_REQUIRED"}
        last_claim = next((event.attributes.get("claim_id") for event in reversed(events) if event.attributes.get("claim_id")), None)
        if events[-1].event_type not in terminal:
            self.state = WorkerState.RECOVERY_REQUIRED
            claim_id = str(last_claim or "unknown")
            self.dlq.enqueue(f"crash-{claim_id}", claim_id, DlqReason.CRASH_RECOVERY, 0, now, "worker terminated during active claim")
            self.journal.append("CRASH_RECOVERY_REQUIRED", self.state, now, {"claim_id": claim_id})
        else:
            self.state = events[-1].state
        return self.state

    def _authorize(self, claim: SignedWorkClaimV104, attestation: WorkerAttestationV104, now: datetime) -> None:
        self.keyring.verify_claim(claim)
        self.keyring.verify_attestation(attestation)
        if claim.generation != self.policy.generation or attestation.generation != self.policy.generation:
            raise AuthorizationError("generation mismatch")
        if claim.fencing_token <= 0:
            raise AuthorizationError("invalid fencing token")
        if claim.policy_digest != self.policy.digest or attestation.policy_digest != self.policy.digest:
            raise AuthorizationError("policy digest mismatch")
        if claim.worker_id != attestation.worker_id or claim.deployment_id != attestation.deployment_id:
            raise AuthorizationError("worker attestation identity mismatch")
        if claim.endpoints != tuple(dict.fromkeys(claim.endpoints)) or not set(claim.endpoints) <= set(self.policy.allowed_endpoints):
            raise AuthorizationError("claim endpoint allowlist violation")
        issued, not_before, expires = map(_utc, (claim.issued_at, claim.not_before, claim.expires_at))
        if issued > not_before or expires <= not_before:
            raise AuthorizationError("invalid claim time window")
        if expires - issued > self.policy.claim_ttl:
            raise AuthorizationError("claim ttl exceeds policy")
        if now < not_before or now >= expires:
            raise StaleClaimError("claim not active")
        if now >= _utc(attestation.expires_at) or _utc(attestation.created_at) > now + timedelta(seconds=1):
            raise StaleClaimError("attestation stale or from future")


__all__ = [name for name in globals() if name.endswith("V104") or name.endswith("Error")]
