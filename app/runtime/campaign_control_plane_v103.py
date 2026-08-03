from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import hashlib
import hmac
import json
import threading
from typing import Any, Iterable, Mapping, Sequence

UTC = timezone.utc
ZERO_DIGEST = "0" * 64


class ControlPlaneError(RuntimeError):
    """Base error for the Schema 103 control plane."""


class InvalidTransition(ControlPlaneError):
    pass


class StaleGeneration(ControlPlaneError):
    pass


class StaleFencingToken(ControlPlaneError):
    pass


class LeaseUnavailable(ControlPlaneError):
    pass


class IntegrityViolation(ControlPlaneError):
    pass


class MutationNotAllowed(ControlPlaneError):
    pass


class CampaignState(str, Enum):
    CREATED = "CREATED"
    READY = "READY"
    LEASED = "LEASED"
    PROBING = "PROBING"
    UPLOADING = "UPLOADING"
    BLOCKED = "BLOCKED"
    QUARANTINED = "QUARANTINED"
    RETIRED = "RETIRED"


class ProbeOutcome(str, Enum):
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    ERROR = "ERROR"


class UploadState(str, Enum):
    EMPTY = "EMPTY"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETE = "COMPLETE"
    CORRUPT = "CORRUPT"


class IncidentSeverity(int, Enum):
    INFO = 10
    WARNING = 20
    CRITICAL = 30


class IncidentStatus(str, Enum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"


class WorkerHealth(str, Enum):
    HEALTHY = "HEALTHY"
    STALE = "STALE"
    DEAD = "DEAD"


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _canonical_json(value: Mapping[str, Any] | Sequence[Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=_json_default,
    ).encode("utf-8")


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return _require_utc(value, "datetime").isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"unsupported canonical JSON value: {type(value)!r}")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_digest(value: str, field_name: str) -> None:
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class ControlPlanePolicyV103:
    campaign_id: str
    generation: int
    starts_at: datetime
    ends_at: datetime
    run_interval: timedelta
    lease_ttl: timedelta
    heartbeat_ttl: timedelta
    probe_timeout: timedelta
    evidence_chunk_bytes: int
    evidence_retention: timedelta
    maximum_open_incidents: int = 8
    allowed_read_only_hosts: tuple[str, ...] = ("paper-api.alpaca.markets",)
    external_order_routing_allowed: bool = False
    live_trading_allowed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "starts_at", _require_utc(self.starts_at, "starts_at"))
        object.__setattr__(self, "ends_at", _require_utc(self.ends_at, "ends_at"))
        if not self.campaign_id.strip():
            raise ValueError("campaign_id is required")
        if self.generation <= 0:
            raise ValueError("generation must be positive")
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        for name in ("run_interval", "lease_ttl", "heartbeat_ttl", "probe_timeout", "evidence_retention"):
            if getattr(self, name) <= timedelta(0):
                raise ValueError(f"{name} must be positive")
        if self.heartbeat_ttl >= self.lease_ttl:
            raise ValueError("heartbeat_ttl must be shorter than lease_ttl")
        if self.evidence_chunk_bytes < 256:
            raise ValueError("evidence_chunk_bytes must be at least 256")
        if self.maximum_open_incidents < 1:
            raise ValueError("maximum_open_incidents must be positive")
        if not self.allowed_read_only_hosts:
            raise ValueError("at least one read-only host is required")
        if self.external_order_routing_allowed or self.live_trading_allowed:
            raise MutationNotAllowed("Schema 103 cannot enable order routing or live trading")

    @property
    def digest(self) -> str:
        return sha256_hex(_canonical_json(asdict(self)))


@dataclass(frozen=True, slots=True)
class LeaseReceiptV103:
    campaign_id: str
    owner_id: str
    generation: int
    fencing_token: int
    acquired_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "acquired_at", _require_utc(self.acquired_at, "acquired_at"))
        object.__setattr__(self, "expires_at", _require_utc(self.expires_at, "expires_at"))
        if self.fencing_token <= 0:
            raise ValueError("fencing_token must be positive")
        if self.expires_at <= self.acquired_at:
            raise ValueError("lease expiration must follow acquisition")


@dataclass(frozen=True, slots=True)
class WorkerHeartbeatV103:
    owner_id: str
    generation: int
    fencing_token: int
    observed_at: datetime
    deployment_id: str
    build_identity: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", _require_utc(self.observed_at, "observed_at"))
        if not self.deployment_id or not self.build_identity:
            raise ValueError("deployment_id and build_identity are required")


@dataclass(frozen=True, slots=True)
class ReadOnlyProbePlanV103:
    run_id: str
    request_id: str
    campaign_id: str
    generation: int
    account_id: str
    host: str
    method: str
    path: str
    created_at: datetime
    deadline_at: datetime
    mutation_requested: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", _require_utc(self.created_at, "created_at"))
        object.__setattr__(self, "deadline_at", _require_utc(self.deadline_at, "deadline_at"))
        if not all((self.run_id, self.request_id, self.campaign_id, self.account_id, self.host, self.path)):
            raise ValueError("probe identity fields are required")
        if self.generation <= 0:
            raise ValueError("generation must be positive")
        if self.deadline_at <= self.created_at:
            raise ValueError("deadline_at must follow created_at")
        if self.method.upper() not in {"GET", "HEAD"}:
            raise MutationNotAllowed("only GET and HEAD are allowed")
        if self.mutation_requested:
            raise MutationNotAllowed("probe mutation_requested must be false")

    @property
    def digest(self) -> str:
        return sha256_hex(_canonical_json(asdict(self)))


@dataclass(frozen=True, slots=True)
class ReadOnlyProbeEvidenceV103:
    run_id: str
    request_id: str
    campaign_id: str
    generation: int
    account_id: str
    observed_at: datetime
    outcome: ProbeOutcome
    account_read_verified: bool
    open_orders_read_verified: bool
    stream_auth_verified: bool
    mutation_count: int
    external_order_routing_attempted: bool
    payload_digest: str
    diagnostic_code: str = "OK"

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", _require_utc(self.observed_at, "observed_at"))
        _validate_digest(self.payload_digest, "payload_digest")
        if self.generation <= 0:
            raise ValueError("generation must be positive")
        if self.mutation_count < 0:
            raise ValueError("mutation_count cannot be negative")
        if self.mutation_count or self.external_order_routing_attempted:
            raise MutationNotAllowed("read-only evidence cannot contain broker mutations")
        if self.outcome is ProbeOutcome.VERIFIED and not (
            self.account_read_verified
            and self.open_orders_read_verified
            and self.stream_auth_verified
        ):
            raise ValueError("VERIFIED evidence requires all read-only checks")

    @property
    def digest(self) -> str:
        return sha256_hex(_canonical_json(asdict(self)))


@dataclass(frozen=True, slots=True)
class EvidenceManifestV103:
    upload_id: str
    run_id: str
    campaign_id: str
    generation: int
    total_size: int
    chunk_size: int
    expected_digest: str
    next_offset: int
    state: UploadState
    created_at: datetime
    updated_at: datetime
    retention_until: datetime
    previous_manifest_digest: str
    manifest_digest: str
    legal_hold: bool = False

    def __post_init__(self) -> None:
        for name in ("created_at", "updated_at", "retention_until"):
            object.__setattr__(self, name, _require_utc(getattr(self, name), name))
        _validate_digest(self.expected_digest, "expected_digest")
        _validate_digest(self.previous_manifest_digest, "previous_manifest_digest")
        _validate_digest(self.manifest_digest, "manifest_digest")
        if self.total_size < 0 or self.next_offset < 0 or self.next_offset > self.total_size:
            raise ValueError("invalid upload size or offset")
        if self.chunk_size < 256:
            raise ValueError("chunk_size must be at least 256")


@dataclass(frozen=True, slots=True)
class IncidentV103:
    incident_id: str
    dedupe_key: str
    campaign_id: str
    run_id: str | None
    severity: IncidentSeverity
    status: IncidentStatus
    code: str
    details_digest: str
    opened_at: datetime
    updated_at: datetime
    acknowledged_by: str | None = None
    resolved_by: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "opened_at", _require_utc(self.opened_at, "opened_at"))
        object.__setattr__(self, "updated_at", _require_utc(self.updated_at, "updated_at"))
        _validate_digest(self.details_digest, "details_digest")


@dataclass(frozen=True, slots=True)
class ControlPlaneEventV103:
    sequence: int
    campaign_id: str
    event_type: str
    from_state: CampaignState
    to_state: CampaignState
    generation: int
    fencing_token: int
    occurred_at: datetime
    attributes: Mapping[str, Any]
    previous_digest: str
    event_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at", _require_utc(self.occurred_at, "occurred_at"))
        _validate_digest(self.previous_digest, "previous_digest")
        _validate_digest(self.event_digest, "event_digest")


@dataclass(frozen=True, slots=True)
class ControlPlaneSnapshotV103:
    campaign_id: str
    policy_digest: str
    state: CampaignState
    generation: int
    next_due_at: datetime
    active_run_id: str | None
    lease_owner: str | None
    fencing_token: int
    lease_expires_at: datetime | None
    heartbeat_at: datetime | None
    worker_health: WorkerHealth
    upload_state: UploadState
    evidence_digest: str | None
    open_incidents: int
    critical_incidents: int
    retained_artifacts: int
    event_tail_digest: str
    version: int
    external_order_routing_allowed: bool = False
    live_trading_allowed: bool = False


@dataclass(slots=True)
class _MutableCampaign:
    policy: ControlPlanePolicyV103
    state: CampaignState
    generation: int
    next_due_at: datetime
    active_run_id: str | None = None
    active_probe_digest: str | None = None
    lease_owner: str | None = None
    fencing_token: int = 0
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    heartbeat_deployment_id: str | None = None
    heartbeat_build_identity: str | None = None
    upload_id: str | None = None
    upload_state: UploadState = UploadState.EMPTY
    evidence_digest: str | None = None
    version: int = 0


@dataclass(slots=True)
class _MutableUpload:
    manifest: EvidenceManifestV103
    chunks: dict[int, bytes] = field(default_factory=dict)
    chunk_digests: dict[int, str] = field(default_factory=dict)


class InMemoryControlPlaneStoreV103:
    """Thread-safe deterministic reference store.

    The PostgreSQL migration and adapter provide the production persistence boundary.
    This store is deliberately strict so state-machine and failure behavior can be
    qualified without external infrastructure.
    """

    backend_kind = "memory-reference"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._campaigns: dict[str, _MutableCampaign] = {}
        self._uploads: dict[str, _MutableUpload] = {}
        self._events: dict[str, list[ControlPlaneEventV103]] = {}
        self._incidents: dict[str, IncidentV103] = {}
        self._incident_by_dedupe: dict[str, str] = {}
        self._retained: dict[str, tuple[bytes, datetime, bool, str]] = {}
        self._incident_counter = 0

    def register_campaign(self, policy: ControlPlanePolicyV103, now: datetime) -> ControlPlaneSnapshotV103:
        now = _require_utc(now, "now")
        with self._lock:
            if policy.campaign_id in self._campaigns:
                current = self._campaigns[policy.campaign_id]
                if current.policy.digest != policy.digest:
                    raise IntegrityViolation("campaign already registered with different policy")
                return self._snapshot_unlocked(current, now)
            state = CampaignState.READY if now >= policy.starts_at else CampaignState.CREATED
            campaign = _MutableCampaign(
                policy=policy,
                state=state,
                generation=policy.generation,
                next_due_at=policy.starts_at,
            )
            self._campaigns[policy.campaign_id] = campaign
            self._events[policy.campaign_id] = []
            self._append_event_unlocked(campaign, "CAMPAIGN_REGISTERED", state, now, {"policy_digest": policy.digest})
            return self._snapshot_unlocked(campaign, now)

    def activate_due_campaigns(self, now: datetime) -> tuple[str, ...]:
        now = _require_utc(now, "now")
        activated: list[str] = []
        with self._lock:
            for campaign in self._campaigns.values():
                if campaign.state is CampaignState.CREATED and now >= campaign.policy.starts_at:
                    previous = campaign.state
                    campaign.state = CampaignState.READY
                    campaign.version += 1
                    self._append_event_unlocked(campaign, "CAMPAIGN_ACTIVATED", previous, now, {})
                    activated.append(campaign.policy.campaign_id)
            return tuple(sorted(activated))

    def acquire_lease(
        self,
        campaign_id: str,
        owner_id: str,
        generation: int,
        now: datetime,
    ) -> LeaseReceiptV103:
        now = _require_utc(now, "now")
        if not owner_id.strip():
            raise ValueError("owner_id is required")
        with self._lock:
            campaign = self._campaign(campaign_id)
            self._check_generation(campaign, generation)
            self._recover_stale_unlocked(campaign, now)
            if campaign.state is not CampaignState.READY:
                raise LeaseUnavailable(f"campaign is not ready: {campaign.state.value}")
            if now < campaign.next_due_at:
                raise LeaseUnavailable("campaign is not due")
            if now > campaign.policy.ends_at:
                previous = campaign.state
                campaign.state = CampaignState.RETIRED
                campaign.version += 1
                self._append_event_unlocked(campaign, "CAMPAIGN_RETIRED", previous, now, {})
                raise LeaseUnavailable("campaign window has ended")
            if self._critical_incident_count_unlocked(campaign_id):
                raise LeaseUnavailable("critical incident blocks lease acquisition")
            campaign.fencing_token += 1
            campaign.lease_owner = owner_id
            campaign.lease_expires_at = now + campaign.policy.lease_ttl
            campaign.heartbeat_at = now
            previous = campaign.state
            campaign.state = CampaignState.LEASED
            campaign.version += 1
            self._append_event_unlocked(
                campaign,
                "LEASE_ACQUIRED",
                previous,
                now,
                {"owner_id": owner_id, "fencing_token": campaign.fencing_token},
            )
            return LeaseReceiptV103(
                campaign_id=campaign_id,
                owner_id=owner_id,
                generation=campaign.generation,
                fencing_token=campaign.fencing_token,
                acquired_at=now,
                expires_at=campaign.lease_expires_at,
            )

    def heartbeat(self, campaign_id: str, heartbeat: WorkerHeartbeatV103) -> ControlPlaneSnapshotV103:
        with self._lock:
            campaign = self._campaign(campaign_id)
            self._check_lease(campaign, heartbeat.owner_id, heartbeat.generation, heartbeat.fencing_token, heartbeat.observed_at)
            if campaign.heartbeat_at and heartbeat.observed_at < campaign.heartbeat_at:
                self._quarantine_unlocked(campaign, "HEARTBEAT_TIME_REGRESSION", heartbeat.observed_at)
                raise IntegrityViolation("heartbeat time regression")
            campaign.heartbeat_at = heartbeat.observed_at
            campaign.lease_expires_at = heartbeat.observed_at + campaign.policy.lease_ttl
            campaign.heartbeat_deployment_id = heartbeat.deployment_id
            campaign.heartbeat_build_identity = heartbeat.build_identity
            campaign.version += 1
            self._append_event_unlocked(
                campaign,
                "WORKER_HEARTBEAT",
                campaign.state,
                heartbeat.observed_at,
                {
                    "deployment_id": heartbeat.deployment_id,
                    "build_identity": heartbeat.build_identity,
                },
            )
            return self._snapshot_unlocked(campaign, heartbeat.observed_at)

    def release_lease(
        self,
        campaign_id: str,
        lease: LeaseReceiptV103,
        now: datetime,
    ) -> ControlPlaneSnapshotV103:
        now = _require_utc(now, "now")
        with self._lock:
            campaign = self._campaign(campaign_id)
            self._check_lease(
                campaign,
                lease.owner_id,
                lease.generation,
                lease.fencing_token,
                now,
            )
            if campaign.state is not CampaignState.LEASED or campaign.active_run_id:
                raise InvalidTransition("only an idle LEASED campaign can release its lease")
            previous = campaign.state
            campaign.lease_owner = None
            campaign.lease_expires_at = None
            campaign.heartbeat_at = None
            campaign.heartbeat_deployment_id = None
            campaign.heartbeat_build_identity = None
            campaign.state = CampaignState.READY
            campaign.version += 1
            self._append_event_unlocked(
                campaign,
                "LEASE_RELEASED",
                previous,
                now,
                {"owner_id": lease.owner_id, "fencing_token": lease.fencing_token},
            )
            return self._snapshot_unlocked(campaign, now)

    def begin_read_only_probe(
        self,
        plan: ReadOnlyProbePlanV103,
        lease: LeaseReceiptV103,
        now: datetime,
    ) -> ControlPlaneSnapshotV103:
        now = _require_utc(now, "now")
        with self._lock:
            campaign = self._campaign(plan.campaign_id)
            self._check_lease(campaign, lease.owner_id, lease.generation, lease.fencing_token, now)
            if campaign.state is not CampaignState.LEASED:
                raise InvalidTransition("probe requires LEASED state")
            if plan.generation != campaign.generation:
                raise StaleGeneration("probe generation mismatch")
            if plan.host not in campaign.policy.allowed_read_only_hosts:
                self._quarantine_unlocked(campaign, "READ_ONLY_HOST_NOT_ALLOWED", now)
                raise MutationNotAllowed("probe host is not allowlisted")
            if plan.deadline_at - plan.created_at > campaign.policy.probe_timeout:
                raise ValueError("probe deadline exceeds policy timeout")
            if plan.deadline_at <= now:
                self._block_unlocked(campaign, "PROBE_DEADLINE_EXPIRED", now, plan.run_id)
                raise InvalidTransition("probe deadline expired")
            if campaign.active_run_id and campaign.active_run_id != plan.run_id:
                raise InvalidTransition("another run is active")
            campaign.active_run_id = plan.run_id
            campaign.active_probe_digest = plan.digest
            previous = campaign.state
            campaign.state = CampaignState.PROBING
            campaign.version += 1
            self._append_event_unlocked(
                campaign,
                "READ_ONLY_PROBE_STARTED",
                previous,
                now,
                {"run_id": plan.run_id, "request_id": plan.request_id, "probe_digest": plan.digest},
            )
            return self._snapshot_unlocked(campaign, now)

    def record_probe_evidence(
        self,
        evidence: ReadOnlyProbeEvidenceV103,
        lease: LeaseReceiptV103,
        now: datetime,
    ) -> ControlPlaneSnapshotV103:
        now = _require_utc(now, "now")
        with self._lock:
            campaign = self._campaign(evidence.campaign_id)
            self._check_lease(campaign, lease.owner_id, lease.generation, lease.fencing_token, now)
            if campaign.state is not CampaignState.PROBING:
                raise InvalidTransition("probe evidence requires PROBING state")
            if evidence.run_id != campaign.active_run_id:
                self._quarantine_unlocked(campaign, "PROBE_RUN_ID_MISMATCH", now)
                raise IntegrityViolation("probe run mismatch")
            if evidence.generation != campaign.generation:
                self._quarantine_unlocked(campaign, "PROBE_GENERATION_MISMATCH", now)
                raise IntegrityViolation("probe generation mismatch")
            if evidence.observed_at > now + timedelta(seconds=1):
                self._quarantine_unlocked(campaign, "PROBE_TIME_IN_FUTURE", now)
                raise IntegrityViolation("probe evidence is in the future")
            if evidence.outcome is not ProbeOutcome.VERIFIED:
                self._block_unlocked(campaign, f"PROBE_{evidence.outcome.value}", now, evidence.run_id)
                return self._snapshot_unlocked(campaign, now)
            previous = campaign.state
            campaign.state = CampaignState.UPLOADING
            campaign.upload_state = UploadState.EMPTY
            campaign.version += 1
            self._append_event_unlocked(
                campaign,
                "READ_ONLY_PROBE_VERIFIED",
                previous,
                now,
                {"run_id": evidence.run_id, "evidence_digest": evidence.digest},
            )
            return self._snapshot_unlocked(campaign, now)

    def open_evidence_upload(
        self,
        campaign_id: str,
        run_id: str,
        upload_id: str,
        total_size: int,
        expected_digest: str,
        lease: LeaseReceiptV103,
        now: datetime,
    ) -> EvidenceManifestV103:
        now = _require_utc(now, "now")
        _validate_digest(expected_digest, "expected_digest")
        if total_size <= 0:
            raise ValueError("total_size must be positive")
        with self._lock:
            campaign = self._campaign(campaign_id)
            self._check_lease(campaign, lease.owner_id, lease.generation, lease.fencing_token, now)
            if campaign.state is not CampaignState.UPLOADING or campaign.active_run_id != run_id:
                raise InvalidTransition("upload requires matching active UPLOADING run")
            existing = self._uploads.get(upload_id)
            if existing:
                manifest = existing.manifest
                if (
                    manifest.campaign_id != campaign_id
                    or manifest.run_id != run_id
                    or manifest.total_size != total_size
                    or manifest.expected_digest != expected_digest
                ):
                    self._quarantine_unlocked(campaign, "UPLOAD_IDENTITY_COLLISION", now)
                    raise IntegrityViolation("upload identity collision")
                return manifest
            previous_manifest_digest = self._latest_manifest_digest_unlocked(campaign_id)
            manifest = self._make_manifest(
                upload_id=upload_id,
                run_id=run_id,
                campaign=campaign,
                total_size=total_size,
                expected_digest=expected_digest,
                next_offset=0,
                state=UploadState.IN_PROGRESS,
                created_at=now,
                updated_at=now,
                previous_manifest_digest=previous_manifest_digest,
                legal_hold=False,
            )
            self._uploads[upload_id] = _MutableUpload(manifest=manifest)
            campaign.upload_id = upload_id
            campaign.upload_state = UploadState.IN_PROGRESS
            campaign.version += 1
            self._append_event_unlocked(
                campaign,
                "EVIDENCE_UPLOAD_OPENED",
                campaign.state,
                now,
                {"upload_id": upload_id, "total_size": total_size, "expected_digest": expected_digest},
            )
            return manifest

    def upload_evidence_chunk(
        self,
        campaign_id: str,
        upload_id: str,
        offset: int,
        data: bytes,
        chunk_digest: str,
        lease: LeaseReceiptV103,
        now: datetime,
    ) -> EvidenceManifestV103:
        now = _require_utc(now, "now")
        _validate_digest(chunk_digest, "chunk_digest")
        if not data:
            raise ValueError("chunk data cannot be empty")
        if sha256_hex(data) != chunk_digest:
            raise IntegrityViolation("chunk digest mismatch")
        with self._lock:
            campaign = self._campaign(campaign_id)
            self._check_lease(campaign, lease.owner_id, lease.generation, lease.fencing_token, now)
            upload = self._uploads.get(upload_id)
            if upload is None or campaign.upload_id != upload_id:
                raise InvalidTransition("upload is not active")
            manifest = upload.manifest
            if manifest.state is not UploadState.IN_PROGRESS:
                raise InvalidTransition("upload is not in progress")
            if len(data) > manifest.chunk_size:
                raise ValueError("chunk exceeds configured chunk size")
            if offset < manifest.next_offset:
                previous_digest = upload.chunk_digests.get(offset)
                if previous_digest == chunk_digest and upload.chunks.get(offset) == data:
                    return manifest
                self._quarantine_unlocked(campaign, "UPLOAD_REPLAY_MISMATCH", now)
                raise IntegrityViolation("replayed chunk differs from stored chunk")
            if offset != manifest.next_offset:
                raise InvalidTransition("chunks must be uploaded contiguously")
            if offset + len(data) > manifest.total_size:
                raise ValueError("chunk exceeds total upload size")
            upload.chunks[offset] = bytes(data)
            upload.chunk_digests[offset] = chunk_digest
            new_offset = offset + len(data)
            upload.manifest = self._make_manifest(
                upload_id=manifest.upload_id,
                run_id=manifest.run_id,
                campaign=campaign,
                total_size=manifest.total_size,
                expected_digest=manifest.expected_digest,
                next_offset=new_offset,
                state=UploadState.IN_PROGRESS,
                created_at=manifest.created_at,
                updated_at=now,
                previous_manifest_digest=manifest.previous_manifest_digest,
                legal_hold=manifest.legal_hold,
            )
            campaign.version += 1
            self._append_event_unlocked(
                campaign,
                "EVIDENCE_CHUNK_ACCEPTED",
                campaign.state,
                now,
                {"upload_id": upload_id, "offset": offset, "size": len(data), "chunk_digest": chunk_digest},
            )
            return upload.manifest

    def finalize_evidence_upload(
        self,
        campaign_id: str,
        upload_id: str,
        lease: LeaseReceiptV103,
        now: datetime,
    ) -> ControlPlaneSnapshotV103:
        now = _require_utc(now, "now")
        with self._lock:
            campaign = self._campaign(campaign_id)
            self._check_lease(campaign, lease.owner_id, lease.generation, lease.fencing_token, now)
            upload = self._uploads.get(upload_id)
            if upload is None or campaign.upload_id != upload_id:
                raise InvalidTransition("upload is not active")
            manifest = upload.manifest
            if manifest.state is UploadState.COMPLETE:
                return self._snapshot_unlocked(campaign, now)
            if manifest.next_offset != manifest.total_size:
                raise InvalidTransition("upload is incomplete")
            ordered = b"".join(upload.chunks[offset] for offset in sorted(upload.chunks))
            if len(ordered) != manifest.total_size or sha256_hex(ordered) != manifest.expected_digest:
                upload.manifest = replace(manifest, state=UploadState.CORRUPT, updated_at=now)
                campaign.upload_state = UploadState.CORRUPT
                self._quarantine_unlocked(campaign, "EVIDENCE_FINAL_DIGEST_MISMATCH", now)
                raise IntegrityViolation("final evidence digest mismatch")
            upload.manifest = self._make_manifest(
                upload_id=manifest.upload_id,
                run_id=manifest.run_id,
                campaign=campaign,
                total_size=manifest.total_size,
                expected_digest=manifest.expected_digest,
                next_offset=manifest.total_size,
                state=UploadState.COMPLETE,
                created_at=manifest.created_at,
                updated_at=now,
                previous_manifest_digest=manifest.previous_manifest_digest,
                legal_hold=manifest.legal_hold,
            )
            self._retained[upload_id] = (
                ordered,
                upload.manifest.retention_until,
                upload.manifest.legal_hold,
                upload.manifest.manifest_digest,
            )
            campaign.upload_state = UploadState.COMPLETE
            campaign.evidence_digest = manifest.expected_digest
            campaign.active_run_id = None
            campaign.active_probe_digest = None
            campaign.upload_id = None
            campaign.lease_owner = None
            campaign.lease_expires_at = None
            campaign.heartbeat_at = None
            previous = campaign.state
            campaign.next_due_at = max(campaign.next_due_at + campaign.policy.run_interval, now + campaign.policy.run_interval)
            campaign.state = CampaignState.RETIRED if campaign.next_due_at > campaign.policy.ends_at else CampaignState.READY
            campaign.version += 1
            self._append_event_unlocked(
                campaign,
                "EVIDENCE_UPLOAD_VERIFIED",
                previous,
                now,
                {"upload_id": upload_id, "evidence_digest": manifest.expected_digest},
            )
            return self._snapshot_unlocked(campaign, now)

    def set_legal_hold(self, upload_id: str, enabled: bool, operator_id: str, now: datetime) -> EvidenceManifestV103:
        now = _require_utc(now, "now")
        if not operator_id:
            raise ValueError("operator_id is required")
        with self._lock:
            upload = self._uploads.get(upload_id)
            if upload is None:
                raise KeyError(upload_id)
            manifest = upload.manifest
            campaign = self._campaign(manifest.campaign_id)
            upload.manifest = self._make_manifest(
                upload_id=manifest.upload_id,
                run_id=manifest.run_id,
                campaign=campaign,
                total_size=manifest.total_size,
                expected_digest=manifest.expected_digest,
                next_offset=manifest.next_offset,
                state=manifest.state,
                created_at=manifest.created_at,
                updated_at=now,
                previous_manifest_digest=manifest.previous_manifest_digest,
                legal_hold=enabled,
            )
            retained = self._retained.get(upload_id)
            if retained:
                self._retained[upload_id] = (retained[0], retained[1], enabled, upload.manifest.manifest_digest)
            self._append_event_unlocked(
                campaign,
                "EVIDENCE_LEGAL_HOLD_CHANGED",
                campaign.state,
                now,
                {"upload_id": upload_id, "enabled": enabled, "operator_id": operator_id},
            )
            return upload.manifest

    def retention_sweep(self, now: datetime, *, dry_run: bool = False) -> tuple[str, ...]:
        now = _require_utc(now, "now")
        deleted: list[str] = []
        with self._lock:
            for upload_id, (data, retention_until, legal_hold, manifest_digest) in list(self._retained.items()):
                if legal_hold or retention_until > now:
                    continue
                upload = self._uploads[upload_id]
                if upload.manifest.manifest_digest != manifest_digest:
                    campaign = self._campaign(upload.manifest.campaign_id)
                    self._quarantine_unlocked(campaign, "RETENTION_MANIFEST_MISMATCH", now)
                    raise IntegrityViolation("retention manifest mismatch")
                deleted.append(upload_id)
                if not dry_run:
                    del self._retained[upload_id]
                    campaign = self._campaign(upload.manifest.campaign_id)
                    self._append_event_unlocked(
                        campaign,
                        "EVIDENCE_RETAINED_BYTES_DELETED",
                        campaign.state,
                        now,
                        {
                            "upload_id": upload_id,
                            "size": len(data),
                            "manifest_digest": manifest_digest,
                        },
                    )
            return tuple(sorted(deleted))

    def raise_incident(
        self,
        campaign_id: str,
        code: str,
        severity: IncidentSeverity,
        details: Mapping[str, Any],
        now: datetime,
        run_id: str | None = None,
    ) -> IncidentV103:
        now = _require_utc(now, "now")
        with self._lock:
            campaign = self._campaign(campaign_id)
            return self._raise_incident_unlocked(campaign, code, severity, details, now, run_id)

    def acknowledge_incident(self, incident_id: str, operator_id: str, now: datetime) -> IncidentV103:
        now = _require_utc(now, "now")
        if not operator_id:
            raise ValueError("operator_id is required")
        with self._lock:
            incident = self._incidents[incident_id]
            if incident.status is IncidentStatus.RESOLVED:
                return incident
            updated = replace(
                incident,
                status=IncidentStatus.ACKNOWLEDGED,
                acknowledged_by=operator_id,
                updated_at=now,
            )
            self._incidents[incident_id] = updated
            campaign = self._campaign(incident.campaign_id)
            self._append_event_unlocked(
                campaign,
                "INCIDENT_ACKNOWLEDGED",
                campaign.state,
                now,
                {"incident_id": incident_id, "operator_id": operator_id},
            )
            return updated

    def resolve_incident(self, incident_id: str, operator_id: str, now: datetime) -> IncidentV103:
        now = _require_utc(now, "now")
        if not operator_id:
            raise ValueError("operator_id is required")
        with self._lock:
            incident = self._incidents[incident_id]
            updated = replace(
                incident,
                status=IncidentStatus.RESOLVED,
                resolved_by=operator_id,
                updated_at=now,
            )
            self._incidents[incident_id] = updated
            campaign = self._campaign(incident.campaign_id)
            self._append_event_unlocked(
                campaign,
                "INCIDENT_RESOLVED",
                campaign.state,
                now,
                {"incident_id": incident_id, "operator_id": operator_id},
            )
            return updated

    def operator_confirm_cleanup(
        self,
        campaign_id: str,
        operator_id: str,
        cleanup_digest: str,
        residual_order_count: int,
        residual_position_quantity: str,
        now: datetime,
    ) -> ControlPlaneSnapshotV103:
        now = _require_utc(now, "now")
        _validate_digest(cleanup_digest, "cleanup_digest")
        if not operator_id:
            raise ValueError("operator_id is required")
        if residual_order_count < 0:
            raise ValueError("residual_order_count cannot be negative")
        try:
            residual_quantity = Decimal(residual_position_quantity)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("residual_position_quantity must be numeric") from exc
        if residual_order_count != 0 or residual_quantity != Decimal("0"):
            raise InvalidTransition("residual broker state prevents cleanup confirmation")
        with self._lock:
            campaign = self._campaign(campaign_id)
            if campaign.state is not CampaignState.BLOCKED:
                raise InvalidTransition("cleanup confirmation requires BLOCKED state")
            if campaign.upload_id:
                upload = self._uploads.get(campaign.upload_id)
                if upload and upload.manifest.state is UploadState.IN_PROGRESS:
                    manifest = upload.manifest
                    upload.manifest = self._make_manifest(
                        upload_id=manifest.upload_id,
                        run_id=manifest.run_id,
                        campaign=campaign,
                        total_size=manifest.total_size,
                        expected_digest=manifest.expected_digest,
                        next_offset=manifest.next_offset,
                        state=UploadState.CORRUPT,
                        created_at=manifest.created_at,
                        updated_at=now,
                        previous_manifest_digest=manifest.previous_manifest_digest,
                        legal_hold=manifest.legal_hold,
                    )
            campaign.active_run_id = None
            campaign.active_probe_digest = None
            campaign.upload_id = None
            campaign.upload_state = UploadState.EMPTY
            campaign.evidence_digest = None
            campaign.version += 1
            self._append_event_unlocked(
                campaign,
                "OPERATOR_CLEANUP_CONFIRMED",
                campaign.state,
                now,
                {
                    "operator_id": operator_id,
                    "cleanup_digest": cleanup_digest,
                    "residual_order_count": residual_order_count,
                    "residual_position_quantity": str(residual_quantity),
                },
            )
            return self._snapshot_unlocked(campaign, now)

    def operator_release_block(
        self,
        campaign_id: str,
        operator_id: str,
        reason: str,
        expected_generation: int,
        now: datetime,
    ) -> ControlPlaneSnapshotV103:
        now = _require_utc(now, "now")
        if not operator_id or not reason:
            raise ValueError("operator_id and reason are required")
        with self._lock:
            campaign = self._campaign(campaign_id)
            self._check_generation(campaign, expected_generation)
            if campaign.state is not CampaignState.BLOCKED:
                raise InvalidTransition("only BLOCKED campaigns can be released")
            if campaign.active_run_id or campaign.upload_state is UploadState.IN_PROGRESS:
                raise InvalidTransition("active or incomplete run prevents release")
            if self._critical_incident_count_unlocked(campaign_id):
                raise InvalidTransition("critical incidents must be resolved first")
            previous = campaign.state
            campaign.generation += 1
            campaign.state = CampaignState.READY
            campaign.next_due_at = max(now, campaign.next_due_at)
            campaign.version += 1
            self._append_event_unlocked(
                campaign,
                "OPERATOR_BLOCK_RELEASED",
                previous,
                now,
                {"operator_id": operator_id, "reason": reason, "new_generation": campaign.generation},
            )
            return self._snapshot_unlocked(campaign, now)

    def recover_stale_workers(self, now: datetime) -> tuple[str, ...]:
        now = _require_utc(now, "now")
        recovered: list[str] = []
        with self._lock:
            for campaign in self._campaigns.values():
                previous = campaign.state
                self._recover_stale_unlocked(campaign, now)
                if campaign.state is CampaignState.BLOCKED and previous is not CampaignState.BLOCKED:
                    recovered.append(campaign.policy.campaign_id)
            return tuple(sorted(recovered))

    def snapshot(self, campaign_id: str, now: datetime) -> ControlPlaneSnapshotV103:
        now = _require_utc(now, "now")
        with self._lock:
            campaign = self._campaign(campaign_id)
            return self._snapshot_unlocked(campaign, now)

    def readiness(self, campaign_id: str, now: datetime) -> dict[str, Any]:
        now = _require_utc(now, "now")
        with self._lock:
            campaign = self._campaign(campaign_id)
            snapshot = self._snapshot_unlocked(campaign, now)
            reasons: list[str] = []
            if snapshot.state is not CampaignState.READY:
                reasons.append(f"state:{snapshot.state.value}")
            if now < snapshot.next_due_at:
                reasons.append("not_due")
            if snapshot.critical_incidents:
                reasons.append("critical_incident")
            if snapshot.worker_health is WorkerHealth.DEAD:
                reasons.append("worker_dead")
            if self.backend_kind != "postgresql":
                reasons.append("production_postgresql_backend_not_verified")
            return {
                "schema": 103,
                "campaign_id": campaign_id,
                "ready_for_read_only_probe": not reasons,
                "reasons": reasons,
                "backend_kind": self.backend_kind,
                "external_order_routing_allowed": False,
                "live_trading_allowed": False,
                "production_scheduler_verified": self.backend_kind == "postgresql",
            }

    def verify_event_chain(self, campaign_id: str) -> bool:
        with self._lock:
            previous = ZERO_DIGEST
            for event in self._events.get(campaign_id, ()):
                if event.previous_digest != previous:
                    return False
                expected = self._event_digest(
                    event.sequence,
                    event.campaign_id,
                    event.event_type,
                    event.from_state,
                    event.to_state,
                    event.generation,
                    event.fencing_token,
                    event.occurred_at,
                    event.attributes,
                    event.previous_digest,
                )
                if not hmac.compare_digest(expected, event.event_digest):
                    return False
                previous = event.event_digest
            return True

    def events(self, campaign_id: str) -> tuple[ControlPlaneEventV103, ...]:
        with self._lock:
            return tuple(self._events.get(campaign_id, ()))

    def incidents(self, campaign_id: str) -> tuple[IncidentV103, ...]:
        with self._lock:
            return tuple(sorted(
                (incident for incident in self._incidents.values() if incident.campaign_id == campaign_id),
                key=lambda incident: incident.opened_at,
            ))

    def upload_manifest(self, upload_id: str) -> EvidenceManifestV103:
        with self._lock:
            return self._uploads[upload_id].manifest

    def tamper_retained_manifest_for_test(self, upload_id: str) -> None:
        with self._lock:
            data, retention, hold, digest = self._retained[upload_id]
            self._retained[upload_id] = (data, retention, hold, "f" * 64 if digest != "f" * 64 else "e" * 64)

    def _campaign(self, campaign_id: str) -> _MutableCampaign:
        try:
            return self._campaigns[campaign_id]
        except KeyError as exc:
            raise KeyError(f"unknown campaign: {campaign_id}") from exc

    def _check_generation(self, campaign: _MutableCampaign, generation: int) -> None:
        if generation != campaign.generation:
            raise StaleGeneration(f"expected generation {campaign.generation}, got {generation}")

    def _check_lease(
        self,
        campaign: _MutableCampaign,
        owner_id: str,
        generation: int,
        fencing_token: int,
        now: datetime,
    ) -> None:
        self._check_generation(campaign, generation)
        if campaign.lease_owner != owner_id:
            raise LeaseUnavailable("lease owner mismatch")
        if campaign.fencing_token != fencing_token:
            raise StaleFencingToken("stale fencing token")
        if campaign.lease_expires_at is None or now >= campaign.lease_expires_at:
            self._recover_stale_unlocked(campaign, now)
            raise LeaseUnavailable("lease expired")

    def _recover_stale_unlocked(self, campaign: _MutableCampaign, now: datetime) -> None:
        if campaign.state not in {CampaignState.LEASED, CampaignState.PROBING, CampaignState.UPLOADING}:
            return
        lease_stale = campaign.lease_expires_at is None or now >= campaign.lease_expires_at
        heartbeat_stale = campaign.heartbeat_at is None or now - campaign.heartbeat_at >= campaign.policy.heartbeat_ttl
        if lease_stale or heartbeat_stale:
            code = "LEASE_EXPIRED" if lease_stale else "WORKER_HEARTBEAT_STALE"
            self._block_unlocked(campaign, code, now, campaign.active_run_id)

    def _block_unlocked(self, campaign: _MutableCampaign, code: str, now: datetime, run_id: str | None) -> None:
        if campaign.state in {CampaignState.QUARANTINED, CampaignState.RETIRED}:
            return
        previous = campaign.state
        campaign.state = CampaignState.BLOCKED
        campaign.lease_owner = None
        campaign.lease_expires_at = None
        campaign.heartbeat_at = None
        campaign.version += 1
        self._append_event_unlocked(campaign, "CAMPAIGN_BLOCKED", previous, now, {"code": code, "run_id": run_id})
        self._raise_incident_unlocked(
            campaign,
            code,
            IncidentSeverity.CRITICAL,
            {"state": previous.value, "run_id": run_id},
            now,
            run_id,
        )

    def _quarantine_unlocked(self, campaign: _MutableCampaign, code: str, now: datetime) -> None:
        if campaign.state is CampaignState.QUARANTINED:
            return
        previous = campaign.state
        campaign.state = CampaignState.QUARANTINED
        campaign.lease_owner = None
        campaign.lease_expires_at = None
        campaign.heartbeat_at = None
        campaign.version += 1
        self._append_event_unlocked(campaign, "CAMPAIGN_QUARANTINED", previous, now, {"code": code})
        self._raise_incident_unlocked(
            campaign,
            code,
            IncidentSeverity.CRITICAL,
            {"state": previous.value},
            now,
            campaign.active_run_id,
        )

    def _raise_incident_unlocked(
        self,
        campaign: _MutableCampaign,
        code: str,
        severity: IncidentSeverity,
        details: Mapping[str, Any],
        now: datetime,
        run_id: str | None,
    ) -> IncidentV103:
        dedupe_key = f"{campaign.policy.campaign_id}:{run_id or '-'}:{code}"
        existing_id = self._incident_by_dedupe.get(dedupe_key)
        details_digest = sha256_hex(_canonical_json(dict(details)))
        if existing_id:
            existing = self._incidents[existing_id]
            if existing.status is not IncidentStatus.RESOLVED:
                updated = replace(
                    existing,
                    severity=max(existing.severity, severity, key=int),
                    details_digest=details_digest,
                    updated_at=now,
                )
                self._incidents[existing_id] = updated
                return updated
        if self._open_incident_count_unlocked(campaign.policy.campaign_id) >= campaign.policy.maximum_open_incidents:
            severity = IncidentSeverity.CRITICAL
            code = "INCIDENT_BUDGET_EXHAUSTED"
            dedupe_key = f"{campaign.policy.campaign_id}:-:{code}"
            existing_id = self._incident_by_dedupe.get(dedupe_key)
            if existing_id and self._incidents[existing_id].status is not IncidentStatus.RESOLVED:
                return self._incidents[existing_id]
        self._incident_counter += 1
        incident_id = f"inc-{self._incident_counter:08d}"
        incident = IncidentV103(
            incident_id=incident_id,
            dedupe_key=dedupe_key,
            campaign_id=campaign.policy.campaign_id,
            run_id=run_id,
            severity=severity,
            status=IncidentStatus.OPEN,
            code=code,
            details_digest=details_digest,
            opened_at=now,
            updated_at=now,
        )
        self._incidents[incident_id] = incident
        self._incident_by_dedupe[dedupe_key] = incident_id
        self._append_event_unlocked(
            campaign,
            "INCIDENT_OPENED",
            campaign.state,
            now,
            {"incident_id": incident_id, "code": code, "severity": severity.name},
        )
        return incident

    def _append_event_unlocked(
        self,
        campaign: _MutableCampaign,
        event_type: str,
        from_state: CampaignState,
        occurred_at: datetime,
        attributes: Mapping[str, Any],
    ) -> ControlPlaneEventV103:
        events = self._events[campaign.policy.campaign_id]
        previous = events[-1].event_digest if events else ZERO_DIGEST
        sequence = len(events) + 1
        digest = self._event_digest(
            sequence,
            campaign.policy.campaign_id,
            event_type,
            from_state,
            campaign.state,
            campaign.generation,
            campaign.fencing_token,
            occurred_at,
            attributes,
            previous,
        )
        event = ControlPlaneEventV103(
            sequence=sequence,
            campaign_id=campaign.policy.campaign_id,
            event_type=event_type,
            from_state=from_state,
            to_state=campaign.state,
            generation=campaign.generation,
            fencing_token=campaign.fencing_token,
            occurred_at=occurred_at,
            attributes=dict(attributes),
            previous_digest=previous,
            event_digest=digest,
        )
        events.append(event)
        return event

    @staticmethod
    def _event_digest(
        sequence: int,
        campaign_id: str,
        event_type: str,
        from_state: CampaignState,
        to_state: CampaignState,
        generation: int,
        fencing_token: int,
        occurred_at: datetime,
        attributes: Mapping[str, Any],
        previous_digest: str,
    ) -> str:
        return sha256_hex(_canonical_json({
            "sequence": sequence,
            "campaign_id": campaign_id,
            "event_type": event_type,
            "from_state": from_state.value,
            "to_state": to_state.value,
            "generation": generation,
            "fencing_token": fencing_token,
            "occurred_at": occurred_at,
            "attributes": dict(attributes),
            "previous_digest": previous_digest,
        }))

    def _make_manifest(
        self,
        *,
        upload_id: str,
        run_id: str,
        campaign: _MutableCampaign,
        total_size: int,
        expected_digest: str,
        next_offset: int,
        state: UploadState,
        created_at: datetime,
        updated_at: datetime,
        previous_manifest_digest: str,
        legal_hold: bool,
    ) -> EvidenceManifestV103:
        payload = {
            "upload_id": upload_id,
            "run_id": run_id,
            "campaign_id": campaign.policy.campaign_id,
            "generation": campaign.generation,
            "total_size": total_size,
            "chunk_size": campaign.policy.evidence_chunk_bytes,
            "expected_digest": expected_digest,
            "next_offset": next_offset,
            "state": state.value,
            "created_at": created_at,
            "updated_at": updated_at,
            "retention_until": created_at + campaign.policy.evidence_retention,
            "previous_manifest_digest": previous_manifest_digest,
            "legal_hold": legal_hold,
        }
        return EvidenceManifestV103(
            upload_id=upload_id,
            run_id=run_id,
            campaign_id=campaign.policy.campaign_id,
            generation=campaign.generation,
            total_size=total_size,
            chunk_size=campaign.policy.evidence_chunk_bytes,
            expected_digest=expected_digest,
            next_offset=next_offset,
            state=state,
            created_at=created_at,
            updated_at=updated_at,
            retention_until=created_at + campaign.policy.evidence_retention,
            previous_manifest_digest=previous_manifest_digest,
            manifest_digest=sha256_hex(_canonical_json(payload)),
            legal_hold=legal_hold,
        )

    def _latest_manifest_digest_unlocked(self, campaign_id: str) -> str:
        manifests = [
            upload.manifest
            for upload in self._uploads.values()
            if upload.manifest.campaign_id == campaign_id
        ]
        if not manifests:
            return ZERO_DIGEST
        return max(manifests, key=lambda item: item.updated_at).manifest_digest

    def _open_incident_count_unlocked(self, campaign_id: str) -> int:
        return sum(
            incident.campaign_id == campaign_id and incident.status is not IncidentStatus.RESOLVED
            for incident in self._incidents.values()
        )

    def _critical_incident_count_unlocked(self, campaign_id: str) -> int:
        return sum(
            incident.campaign_id == campaign_id
            and incident.status is not IncidentStatus.RESOLVED
            and incident.severity is IncidentSeverity.CRITICAL
            for incident in self._incidents.values()
        )

    def _snapshot_unlocked(self, campaign: _MutableCampaign, now: datetime) -> ControlPlaneSnapshotV103:
        if campaign.heartbeat_at is None:
            health = WorkerHealth.DEAD if campaign.state in {CampaignState.LEASED, CampaignState.PROBING, CampaignState.UPLOADING} else WorkerHealth.HEALTHY
        else:
            age = now - campaign.heartbeat_at
            if age < campaign.policy.heartbeat_ttl:
                health = WorkerHealth.HEALTHY
            elif campaign.lease_expires_at and now < campaign.lease_expires_at:
                health = WorkerHealth.STALE
            else:
                health = WorkerHealth.DEAD
        events = self._events[campaign.policy.campaign_id]
        return ControlPlaneSnapshotV103(
            campaign_id=campaign.policy.campaign_id,
            policy_digest=campaign.policy.digest,
            state=campaign.state,
            generation=campaign.generation,
            next_due_at=campaign.next_due_at,
            active_run_id=campaign.active_run_id,
            lease_owner=campaign.lease_owner,
            fencing_token=campaign.fencing_token,
            lease_expires_at=campaign.lease_expires_at,
            heartbeat_at=campaign.heartbeat_at,
            worker_health=health,
            upload_state=campaign.upload_state,
            evidence_digest=campaign.evidence_digest,
            open_incidents=self._open_incident_count_unlocked(campaign.policy.campaign_id),
            critical_incidents=self._critical_incident_count_unlocked(campaign.policy.campaign_id),
            retained_artifacts=sum(
                upload.manifest.campaign_id == campaign.policy.campaign_id
                for upload in self._uploads.values()
                if upload.manifest.state is UploadState.COMPLETE
            ),
            event_tail_digest=events[-1].event_digest if events else ZERO_DIGEST,
            version=campaign.version,
        )


class ReadOnlyCampaignControlPlaneV103:
    """Thin orchestration façade over a strict store boundary."""

    def __init__(self, store: InMemoryControlPlaneStoreV103) -> None:
        self.store = store

    def scheduler_tick(self, now: datetime) -> dict[str, tuple[str, ...]]:
        activated = self.store.activate_due_campaigns(now)
        blocked = self.store.recover_stale_workers(now)
        return {"activated": activated, "blocked": blocked}

    def run_read_only_probe(
        self,
        plan: ReadOnlyProbePlanV103,
        lease: LeaseReceiptV103,
        evidence: ReadOnlyProbeEvidenceV103,
        evidence_bytes: bytes,
        upload_id: str,
        now: datetime,
    ) -> ControlPlaneSnapshotV103:
        if evidence.run_id != plan.run_id or evidence.request_id != plan.request_id:
            raise IntegrityViolation("plan and evidence identity mismatch")
        self.store.begin_read_only_probe(plan, lease, now)
        snapshot = self.store.record_probe_evidence(evidence, lease, now)
        if snapshot.state is not CampaignState.UPLOADING:
            return snapshot
        digest = sha256_hex(evidence_bytes)
        manifest = self.store.open_evidence_upload(
            plan.campaign_id,
            plan.run_id,
            upload_id,
            len(evidence_bytes),
            digest,
            lease,
            now,
        )
        offset = manifest.next_offset
        while offset < len(evidence_bytes):
            chunk = evidence_bytes[offset : offset + manifest.chunk_size]
            manifest = self.store.upload_evidence_chunk(
                plan.campaign_id,
                upload_id,
                offset,
                chunk,
                sha256_hex(chunk),
                lease,
                now,
            )
            offset = manifest.next_offset
        return self.store.finalize_evidence_upload(plan.campaign_id, upload_id, lease, now)


def deterministic_evidence_bytes(evidence: ReadOnlyProbeEvidenceV103) -> bytes:
    return _canonical_json(asdict(evidence))


def build_verified_probe_evidence(
    plan: ReadOnlyProbePlanV103,
    observed_at: datetime,
    payload: Mapping[str, Any],
) -> ReadOnlyProbeEvidenceV103:
    return ReadOnlyProbeEvidenceV103(
        run_id=plan.run_id,
        request_id=plan.request_id,
        campaign_id=plan.campaign_id,
        generation=plan.generation,
        account_id=plan.account_id,
        observed_at=observed_at,
        outcome=ProbeOutcome.VERIFIED,
        account_read_verified=True,
        open_orders_read_verified=True,
        stream_auth_verified=True,
        mutation_count=0,
        external_order_routing_attempted=False,
        payload_digest=sha256_hex(_canonical_json(dict(payload))),
    )
