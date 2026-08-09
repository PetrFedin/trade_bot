from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import threading
from typing import Protocol

from app.runtime.sandbox_qualification_v101 import Result as QualificationResultV101

UTC = timezone.utc
ZERO_DIGEST = "0" * 64


class SoakError(RuntimeError):
    pass


class SoakCorruption(SoakError):
    pass


class SoakBlocked(SoakError):
    pass


class SoakNotDue(SoakError):
    pass


class StaleSoakLease(SoakError):
    pass


class CampaignState(str, Enum):
    CREATED = "CREATED"
    ACTIVE = "ACTIVE"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    QUARANTINED = "QUARANTINED"


class CampaignEventType(str, Enum):
    CAMPAIGN_STARTED = "CAMPAIGN_STARTED"
    RUN_STARTED = "RUN_STARTED"
    RUN_VERIFIED = "RUN_VERIFIED"
    RUN_FAILED = "RUN_FAILED"
    RUN_MISSED = "RUN_MISSED"
    CAMPAIGN_COMPLETED = "CAMPAIGN_COMPLETED"
    CAMPAIGN_BLOCKED = "CAMPAIGN_BLOCKED"
    CAMPAIGN_QUARANTINED = "CAMPAIGN_QUARANTINED"


class RunOutcome(str, Enum):
    VERIFIED_CLEAN = "VERIFIED_CLEAN"
    PREFLIGHT_BLOCKED = "PREFLIGHT_BLOCKED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    RESIDUAL_PAPER_EXPOSURE = "RESIDUAL_PAPER_EXPOSURE"
    QUARANTINED = "QUARANTINED"
    MISSED_WINDOW = "MISSED_WINDOW"


TRANSITIONS: Mapping[tuple[CampaignState, CampaignEventType], set[CampaignState]] = {
    (CampaignState.CREATED, CampaignEventType.CAMPAIGN_STARTED): {CampaignState.ACTIVE},
    (CampaignState.ACTIVE, CampaignEventType.RUN_STARTED): {CampaignState.RUNNING},
    (CampaignState.ACTIVE, CampaignEventType.RUN_MISSED): {CampaignState.ACTIVE},
    (CampaignState.ACTIVE, CampaignEventType.CAMPAIGN_COMPLETED): {CampaignState.COMPLETED},
    (CampaignState.ACTIVE, CampaignEventType.CAMPAIGN_BLOCKED): {CampaignState.BLOCKED},
    (CampaignState.ACTIVE, CampaignEventType.CAMPAIGN_QUARANTINED): {CampaignState.QUARANTINED},
    (CampaignState.RUNNING, CampaignEventType.RUN_VERIFIED): {CampaignState.ACTIVE},
    (CampaignState.RUNNING, CampaignEventType.RUN_FAILED): {CampaignState.ACTIVE},
    (CampaignState.RUNNING, CampaignEventType.CAMPAIGN_BLOCKED): {CampaignState.BLOCKED},
    (CampaignState.RUNNING, CampaignEventType.CAMPAIGN_QUARANTINED): {CampaignState.QUARANTINED},
}


def require_aware(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _normalize(value: object) -> object:
    if isinstance(value, datetime):
        return require_aware(value, field="datetime").isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "__dataclass_fields__"):
        return _normalize(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _normalize(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (tuple, list, set, frozenset)):
        items = [_normalize(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True)) if isinstance(value, (set, frozenset)) else items
    return value


def canonical_json(value: object) -> str:
    return json.dumps(_normalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    try:
        descriptor = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class SoakCampaignPlanV102:
    campaign_id: str
    generation: int
    starts_at: datetime
    ends_at: datetime
    interval: timedelta
    schedule_grace: timedelta
    lease_ttl: timedelta
    evidence_max_age: timedelta
    evidence_retention: timedelta
    maximum_runs: int
    minimum_verified_runs: int
    maximum_total_failures: int
    maximum_consecutive_failures: int
    plan_digest: str = ""
    external_order_routing_allowed: bool = False
    live_trading_allowed: bool = False

    def validate(self) -> None:
        starts = require_aware(self.starts_at, field="starts_at")
        ends = require_aware(self.ends_at, field="ends_at")
        if not self.campaign_id.strip():
            raise ValueError("campaign_id is required")
        if self.generation <= 0:
            raise ValueError("generation must be positive")
        if ends <= starts:
            raise ValueError("ends_at must be after starts_at")
        for name, value in (
            ("interval", self.interval),
            ("schedule_grace", self.schedule_grace),
            ("lease_ttl", self.lease_ttl),
            ("evidence_max_age", self.evidence_max_age),
            ("evidence_retention", self.evidence_retention),
        ):
            if value <= timedelta(0):
                raise ValueError(f"{name} must be positive")
        if self.schedule_grace >= self.interval:
            raise ValueError("schedule_grace must be shorter than interval")
        if self.evidence_retention < self.evidence_max_age:
            raise ValueError("evidence_retention must cover evidence_max_age")
        if self.maximum_runs < 1:
            raise ValueError("maximum_runs must be positive")
        if not 1 <= self.minimum_verified_runs <= self.maximum_runs:
            raise ValueError("minimum_verified_runs is outside campaign bounds")
        if self.maximum_total_failures < 0 or self.maximum_consecutive_failures < 0:
            raise ValueError("failure budgets cannot be negative")
        if self.external_order_routing_allowed or self.live_trading_allowed:
            raise ValueError("live routing flags must remain false")
        if self.plan_digest and self.plan_digest != self.computed_digest():
            raise SoakCorruption("campaign plan digest mismatch")

    def computed_digest(self) -> str:
        return digest(replace(self, plan_digest=""))

    def sealed(self) -> "SoakCampaignPlanV102":
        unsigned = replace(self, plan_digest="")
        unsigned.validate()
        return replace(unsigned, plan_digest=unsigned.computed_digest())

    def due_at(self, run_index: int) -> datetime:
        if run_index < 1:
            raise ValueError("run_index must be positive")
        return require_aware(self.starts_at, field="starts_at") + self.interval * (run_index - 1)


@dataclass(frozen=True)
class RunClaimV102:
    campaign_id: str
    run_id: str
    run_index: int
    generation: int
    due_at: datetime
    claimed_at: datetime
    lease_fencing_token: int
    claim_digest: str = ""

    def validate(self) -> None:
        require_aware(self.due_at, field="due_at")
        require_aware(self.claimed_at, field="claimed_at")
        if not self.campaign_id.strip() or not self.run_id.strip():
            raise ValueError("claim identifiers are required")
        if min(self.run_index, self.generation, self.lease_fencing_token) <= 0:
            raise ValueError("claim counters must be positive")
        if self.claim_digest and self.claim_digest != self.computed_digest():
            raise SoakCorruption("run claim digest mismatch")

    def computed_digest(self) -> str:
        return digest(replace(self, claim_digest=""))

    def sealed(self) -> "RunClaimV102":
        unsigned = replace(self, claim_digest="")
        unsigned.validate()
        return replace(unsigned, claim_digest=unsigned.computed_digest())


@dataclass(frozen=True)
class QualificationRunEvidenceV102:
    campaign_id: str
    run_id: str
    run_index: int
    generation: int
    qualification_id: str
    captured_at: datetime
    outcome: RunOutcome
    reasons: tuple[str, ...]
    qualification_tail_digest: str
    read_only_probe_verified: bool
    paper_round_trip_verified: bool
    cleanup_verified: bool
    kill_switch_engaged: bool
    evidence_digest: str = ""
    external_order_routing_allowed: bool = False
    live_trading_allowed: bool = False

    def validate(self) -> None:
        require_aware(self.captured_at, field="captured_at")
        for name, value in (
            ("campaign_id", self.campaign_id),
            ("run_id", self.run_id),
            ("qualification_id", self.qualification_id),
        ):
            if not value.strip():
                raise ValueError(f"{name} is required")
        if min(self.run_index, self.generation) <= 0:
            raise ValueError("run_index and generation must be positive")
        if len(self.qualification_tail_digest) != 64:
            raise ValueError("qualification_tail_digest must be SHA-256")
        if tuple(sorted(set(self.reasons))) != self.reasons:
            raise ValueError("reasons must be sorted and unique")
        if self.external_order_routing_allowed or self.live_trading_allowed:
            raise ValueError("live routing flags must remain false")
        if self.outcome is RunOutcome.VERIFIED_CLEAN:
            if not (
                self.read_only_probe_verified
                and self.paper_round_trip_verified
                and self.cleanup_verified
                and not self.kill_switch_engaged
            ):
                raise ValueError("clean outcome requires complete clean evidence")
        if self.evidence_digest and self.evidence_digest != self.computed_digest():
            raise SoakCorruption("run evidence digest mismatch")

    def computed_digest(self) -> str:
        return digest(replace(self, evidence_digest=""))

    def sealed(self) -> "QualificationRunEvidenceV102":
        unsigned = replace(self, evidence_digest="")
        unsigned.validate()
        return replace(unsigned, evidence_digest=unsigned.computed_digest())

    @classmethod
    def from_v101(
        cls,
        *,
        campaign_id: str,
        run_id: str,
        run_index: int,
        generation: int,
        captured_at: datetime,
        result: QualificationResultV101,
    ) -> "QualificationRunEvidenceV102":
        if result.success and result.cleanup_verified and result.paper_round_trip_verified:
            outcome = RunOutcome.VERIFIED_CLEAN
        elif result.kill_switch_engaged and result.filled_quantity > 0:
            outcome = RunOutcome.RESIDUAL_PAPER_EXPOSURE
        elif result.state.value == "RECOVERING":
            outcome = RunOutcome.RECOVERY_REQUIRED
        elif result.state.value == "QUARANTINED":
            outcome = RunOutcome.QUARANTINED
        else:
            outcome = RunOutcome.PREFLIGHT_BLOCKED
        return cls(
            campaign_id=campaign_id,
            run_id=run_id,
            run_index=run_index,
            generation=generation,
            qualification_id=result.qualification_id,
            captured_at=captured_at,
            outcome=outcome,
            reasons=tuple(sorted(set(result.reasons))),
            qualification_tail_digest=result.journal_tail_digest,
            read_only_probe_verified=result.read_only_probe_verified,
            paper_round_trip_verified=result.paper_round_trip_verified,
            cleanup_verified=result.cleanup_verified,
            kill_switch_engaged=result.kill_switch_engaged,
            external_order_routing_allowed=False,
            live_trading_allowed=False,
        ).sealed()


@dataclass(frozen=True)
class LeaseRecordV102:
    owner_id: str
    generation: int
    fencing_token: int
    acquired_at: datetime
    expires_at: datetime
    released: bool
    lease_digest: str = ""

    def validate(self) -> None:
        acquired = require_aware(self.acquired_at, field="acquired_at")
        expires = require_aware(self.expires_at, field="expires_at")
        if not self.owner_id.strip():
            raise ValueError("owner_id is required")
        if min(self.generation, self.fencing_token) <= 0:
            raise ValueError("lease counters must be positive")
        if expires <= acquired and not self.released:
            raise ValueError("active lease expiry must be after acquisition")
        if self.lease_digest and self.lease_digest != self.computed_digest():
            raise SoakCorruption("lease digest mismatch")

    def computed_digest(self) -> str:
        return digest(replace(self, lease_digest=""))

    def sealed(self) -> "LeaseRecordV102":
        unsigned = replace(self, lease_digest="")
        unsigned.validate()
        return replace(unsigned, lease_digest=unsigned.computed_digest())


class FileLeaseStoreV102:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def load(self) -> LeaseRecordV102 | None:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            record = LeaseRecordV102(
                owner_id=str(raw["owner_id"]),
                generation=int(raw["generation"]),
                fencing_token=int(raw["fencing_token"]),
                acquired_at=datetime.fromisoformat(str(raw["acquired_at"])),
                expires_at=datetime.fromisoformat(str(raw["expires_at"])),
                released=bool(raw["released"]),
                lease_digest=str(raw["lease_digest"]),
            )
            record.validate()
            return record
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            if isinstance(exc, SoakCorruption):
                raise
            raise SoakCorruption("invalid lease record") from exc

    def acquire(
        self, *, owner_id: str, generation: int, now: datetime, ttl: timedelta
    ) -> LeaseRecordV102:
        now = require_aware(now, field="now")
        if not owner_id.strip() or generation <= 0 or ttl <= timedelta(0):
            raise ValueError("owner, generation and positive ttl are required")
        with self._lock:
            current = self.load()
            if current is not None and not current.released and current.expires_at > now:
                if current.owner_id != owner_id or current.generation != generation:
                    raise StaleSoakLease("campaign lease is held by another owner")
                return self.renew(
                    owner_id=owner_id,
                    generation=generation,
                    fencing_token=current.fencing_token,
                    now=now,
                    ttl=ttl,
                )
            token = 1 if current is None else current.fencing_token + 1
            record = LeaseRecordV102(
                owner_id=owner_id.strip(),
                generation=generation,
                fencing_token=token,
                acquired_at=now,
                expires_at=now + ttl,
                released=False,
            ).sealed()
            atomic_write(self.path, canonical_json(record) + "\n")
            return record

    def renew(
        self,
        *,
        owner_id: str,
        generation: int,
        fencing_token: int,
        now: datetime,
        ttl: timedelta,
    ) -> LeaseRecordV102:
        now = require_aware(now, field="now")
        with self._lock:
            current = self.assert_held(
                owner_id=owner_id,
                generation=generation,
                fencing_token=fencing_token,
                now=now,
            )
            record = replace(
                current,
                acquired_at=now,
                expires_at=now + ttl,
                lease_digest="",
            ).sealed()
            atomic_write(self.path, canonical_json(record) + "\n")
            return record

    def assert_held(
        self,
        *,
        owner_id: str,
        generation: int,
        fencing_token: int,
        now: datetime,
    ) -> LeaseRecordV102:
        now = require_aware(now, field="now")
        current = self.load()
        if (
            current is None
            or current.released
            or current.owner_id != owner_id
            or current.generation != generation
            or current.fencing_token != fencing_token
            or current.expires_at <= now
        ):
            raise StaleSoakLease("campaign lease is absent, stale or fenced")
        return current

    def release(
        self,
        *,
        owner_id: str,
        generation: int,
        fencing_token: int,
        now: datetime,
    ) -> LeaseRecordV102:
        now = require_aware(now, field="now")
        with self._lock:
            current = self.assert_held(
                owner_id=owner_id,
                generation=generation,
                fencing_token=fencing_token,
                now=now,
            )
            record = replace(
                current,
                expires_at=now,
                released=True,
                lease_digest="",
            ).sealed()
            atomic_write(self.path, canonical_json(record) + "\n")
            return record


@dataclass(frozen=True)
class CampaignEventV102:
    sequence: int
    campaign_id: str
    event_type: CampaignEventType
    from_state: CampaignState
    to_state: CampaignState
    occurred_at: datetime
    generation: int
    attributes: Mapping[str, object]
    previous_digest: str
    event_digest: str

    def base_document(self) -> Mapping[str, object]:
        return {
            "sequence": self.sequence,
            "campaign_id": self.campaign_id,
            "event_type": self.event_type,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "occurred_at": self.occurred_at,
            "generation": self.generation,
            "attributes": dict(self.attributes),
            "previous_digest": self.previous_digest,
        }


class FileCampaignEventStoreV102:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def append(
        self,
        *,
        campaign_id: str,
        event_type: CampaignEventType,
        from_state: CampaignState,
        to_state: CampaignState,
        occurred_at: datetime,
        generation: int,
        attributes: Mapping[str, object],
    ) -> CampaignEventV102:
        occurred_at = require_aware(occurred_at, field="occurred_at")
        with self._lock:
            events = self.load()
            previous = events[-1] if events else None
            event = CampaignEventV102(
                sequence=1 if previous is None else previous.sequence + 1,
                campaign_id=campaign_id,
                event_type=event_type,
                from_state=from_state,
                to_state=to_state,
                occurred_at=occurred_at,
                generation=generation,
                attributes=dict(attributes),
                previous_digest=ZERO_DIGEST if previous is None else previous.event_digest,
                event_digest="",
            )
            event = replace(event, event_digest=digest(event.base_document()))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(canonical_json({**event.base_document(), "event_digest": event.event_digest}) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return event

    def load(self) -> tuple[CampaignEventV102, ...]:
        if not self.path.exists():
            return ()
        events: list[CampaignEventV102] = []
        previous_digest = ZERO_DIGEST
        previous_time: datetime | None = None
        for line_number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                event = CampaignEventV102(
                    sequence=int(raw["sequence"]),
                    campaign_id=str(raw["campaign_id"]),
                    event_type=CampaignEventType(raw["event_type"]),
                    from_state=CampaignState(raw["from_state"]),
                    to_state=CampaignState(raw["to_state"]),
                    occurred_at=datetime.fromisoformat(str(raw["occurred_at"])),
                    generation=int(raw["generation"]),
                    attributes=dict(raw.get("attributes", {})),
                    previous_digest=str(raw["previous_digest"]),
                    event_digest=str(raw["event_digest"]),
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise SoakCorruption(f"invalid campaign event line {line_number}") from exc
            if event.sequence != len(events) + 1:
                raise SoakCorruption("campaign event sequence gap")
            if event.previous_digest != previous_digest:
                raise SoakCorruption("campaign event hash-chain mismatch")
            if event.event_digest != digest(event.base_document()):
                raise SoakCorruption("campaign event digest mismatch")
            if previous_time is not None and event.occurred_at < previous_time:
                raise SoakCorruption("campaign event time regression")
            if event.to_state not in TRANSITIONS.get((event.from_state, event.event_type), set()):
                raise SoakCorruption("invalid persisted campaign transition")
            if event.attributes.get("external_order_routing_allowed") or event.attributes.get("live_trading_allowed"):
                raise SoakCorruption("forbidden routing flag in campaign journal")
            events.append(event)
            previous_digest = event.event_digest
            previous_time = event.occurred_at
        return tuple(events)

    def verify(self) -> bool:
        try:
            self.load()
            return True
        except SoakCorruption:
            return False


@dataclass(frozen=True)
class EvidenceManifestRecordV102:
    sequence: int
    campaign_id: str
    run_id: str
    evidence_digest: str
    artifact_sha256: str
    stored_at: datetime
    retain_until: datetime
    relative_path: str
    previous_digest: str
    record_digest: str

    def base_document(self) -> Mapping[str, object]:
        return {key: value for key, value in asdict(self).items() if key != "record_digest"}


class FileEvidenceArchiveV102:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.manifest_path = root / "manifest.jsonl"
        self._lock = threading.RLock()

    def save(
        self,
        evidence: QualificationRunEvidenceV102,
        *,
        stored_at: datetime,
        retention: timedelta,
    ) -> EvidenceManifestRecordV102:
        evidence.validate()
        if not evidence.evidence_digest:
            raise ValueError("evidence must be sealed")
        stored_at = require_aware(stored_at, field="stored_at")
        if retention <= timedelta(0):
            raise ValueError("retention must be positive")
        with self._lock:
            records = self.load_manifest()
            relative = Path(evidence.campaign_id) / f"{evidence.run_id}.json"
            artifact = self.root / relative
            payload = canonical_json(evidence) + "\n"
            artifact_sha = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            if artifact.exists():
                existing_sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
                if existing_sha != artifact_sha:
                    raise SoakCorruption("evidence artifact overwrite conflict")
                existing = next((item for item in records if item.run_id == evidence.run_id), None)
                if existing is None:
                    raise SoakCorruption("evidence artifact missing manifest record")
                return existing
            atomic_write(artifact, payload)
            previous = records[-1] if records else None
            record = EvidenceManifestRecordV102(
                sequence=1 if previous is None else previous.sequence + 1,
                campaign_id=evidence.campaign_id,
                run_id=evidence.run_id,
                evidence_digest=evidence.evidence_digest,
                artifact_sha256=artifact_sha,
                stored_at=stored_at,
                retain_until=stored_at + retention,
                relative_path=str(relative),
                previous_digest=ZERO_DIGEST if previous is None else previous.record_digest,
                record_digest="",
            )
            record = replace(record, record_digest=digest(record.base_document()))
            self.root.mkdir(parents=True, exist_ok=True)
            with self.manifest_path.open("a", encoding="utf-8") as handle:
                handle.write(canonical_json({**record.base_document(), "record_digest": record.record_digest}) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return record

    def load_manifest(self) -> tuple[EvidenceManifestRecordV102, ...]:
        if not self.manifest_path.exists():
            return ()
        records: list[EvidenceManifestRecordV102] = []
        previous = ZERO_DIGEST
        for line_number, line in enumerate(self.manifest_path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                raw = json.loads(line)
                record = EvidenceManifestRecordV102(
                    sequence=int(raw["sequence"]),
                    campaign_id=str(raw["campaign_id"]),
                    run_id=str(raw["run_id"]),
                    evidence_digest=str(raw["evidence_digest"]),
                    artifact_sha256=str(raw["artifact_sha256"]),
                    stored_at=datetime.fromisoformat(str(raw["stored_at"])),
                    retain_until=datetime.fromisoformat(str(raw["retain_until"])),
                    relative_path=str(raw["relative_path"]),
                    previous_digest=str(raw["previous_digest"]),
                    record_digest=str(raw["record_digest"]),
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise SoakCorruption(f"invalid evidence manifest line {line_number}") from exc
            if record.sequence != len(records) + 1 or record.previous_digest != previous:
                raise SoakCorruption("evidence manifest chain mismatch")
            if record.record_digest != digest(record.base_document()):
                raise SoakCorruption("evidence manifest digest mismatch")
            artifact = self.root / record.relative_path
            if not artifact.is_file() or hashlib.sha256(artifact.read_bytes()).hexdigest() != record.artifact_sha256:
                raise SoakCorruption("evidence artifact missing or corrupted")
            records.append(record)
            previous = record.record_digest
        return tuple(records)

    def verify(self) -> bool:
        try:
            self.load_manifest()
            return True
        except SoakCorruption:
            return False


@dataclass(frozen=True)
class CampaignSnapshotV102:
    campaign_id: str
    state: CampaignState
    generation: int
    completed_runs: int
    verified_runs: int
    total_failures: int
    consecutive_failures: int
    active_run_id: str
    tail_digest: str
    eligible_for_extended_paper_soak: bool
    external_order_routing_allowed: bool = False
    live_trading_allowed: bool = False


class KillSwitchReader(Protocol):
    def status(self) -> object: ...


class SoakCampaignServiceV102:
    def __init__(
        self,
        *,
        plan: SoakCampaignPlanV102,
        event_store: FileCampaignEventStoreV102,
        lease_store: FileLeaseStoreV102,
        evidence_archive: FileEvidenceArchiveV102,
        kill_switch: KillSwitchReader,
    ) -> None:
        plan.validate()
        if not plan.plan_digest:
            raise ValueError("campaign plan must be sealed")
        if not event_store.verify() or not evidence_archive.verify():
            raise SoakCorruption("campaign evidence stores are invalid")
        self.plan = plan
        self.event_store = event_store
        self.lease_store = lease_store
        self.evidence_archive = evidence_archive
        self.kill_switch = kill_switch
        self._lock = threading.RLock()
        self._replay()

    def _replay(self) -> None:
        self.events = tuple(
            event for event in self.event_store.load() if event.campaign_id == self.plan.campaign_id
        )
        self.state = CampaignState.CREATED
        self.completed_runs = 0
        self.verified_runs = 0
        self.total_failures = 0
        self.consecutive_failures = 0
        self.active_run_id = ""
        self.active_run_index = 0
        for event in self.events:
            if event.generation != self.plan.generation:
                raise SoakCorruption("campaign generation mismatch in journal")
            self.state = event.to_state
            if event.event_type is CampaignEventType.RUN_STARTED:
                if self.active_run_id:
                    raise SoakCorruption("overlapping persisted campaign runs")
                self.active_run_id = str(event.attributes["run_id"])
                self.active_run_index = int(event.attributes["run_index"])
            elif event.event_type is CampaignEventType.RUN_VERIFIED:
                self._clear_active_from_event(event)
                self.completed_runs += 1
                self.verified_runs += 1
                self.consecutive_failures = 0
            elif event.event_type in {CampaignEventType.RUN_FAILED, CampaignEventType.RUN_MISSED}:
                if event.event_type is CampaignEventType.RUN_FAILED:
                    self._clear_active_from_event(event)
                self.completed_runs += 1
                self.total_failures += 1
                self.consecutive_failures += 1
        if self.completed_runs > self.plan.maximum_runs:
            raise SoakCorruption("completed run count exceeds campaign plan")

    def _clear_active_from_event(self, event: CampaignEventV102) -> None:
        if not self.active_run_id or str(event.attributes.get("run_id")) != self.active_run_id:
            raise SoakCorruption("run completion does not match active run")
        self.active_run_id = ""
        self.active_run_index = 0

    def start(
        self,
        *,
        now: datetime,
        owner_id: str,
        fencing_token: int,
    ) -> CampaignSnapshotV102:
        now = require_aware(now, field="now")
        with self._lock:
            self._assert_lease(owner_id, fencing_token, now)
            if self.state is not CampaignState.CREATED:
                raise SoakError(f"campaign cannot start from {self.state.value}")
            if now > self.plan.ends_at:
                raise SoakBlocked("campaign window has already ended")
            self._transition(
                CampaignEventType.CAMPAIGN_STARTED,
                CampaignState.ACTIVE,
                now,
                {"plan_digest": self.plan.plan_digest},
            )
            return self.snapshot()

    def claim_due_run(
        self,
        *,
        now: datetime,
        owner_id: str,
        fencing_token: int,
    ) -> RunClaimV102 | None:
        now = require_aware(now, field="now")
        with self._lock:
            self._assert_lease(owner_id, fencing_token, now)
            if self.state is not CampaignState.ACTIVE:
                raise SoakError(f"run cannot be claimed from {self.state.value}")
            if self._kill_switch_engaged():
                self._block(now, ("KILL_SWITCH_ENGAGED",))
                return None
            if self.completed_runs >= self.plan.maximum_runs:
                self._finalize(now)
                return None
            run_index = self.completed_runs + 1
            due_at = self.plan.due_at(run_index)
            if due_at > self.plan.ends_at:
                self._block(now, ("CAMPAIGN_WINDOW_EXHAUSTED",))
                return None
            if now < due_at:
                raise SoakNotDue(f"next run is due at {due_at.isoformat()}")
            if now > due_at + self.plan.schedule_grace:
                run_id = self._run_id(run_index)
                self._transition(
                    CampaignEventType.RUN_MISSED,
                    CampaignState.ACTIVE,
                    now,
                    {
                        "run_id": run_id,
                        "run_index": run_index,
                        "due_at": due_at,
                        "outcome": RunOutcome.MISSED_WINDOW,
                        "reason": "SCHEDULE_WINDOW_MISSED",
                    },
                )
                self.completed_runs += 1
                self.total_failures += 1
                self.consecutive_failures += 1
                self._enforce_budgets(now)
                return None
            claim = RunClaimV102(
                campaign_id=self.plan.campaign_id,
                run_id=self._run_id(run_index),
                run_index=run_index,
                generation=self.plan.generation,
                due_at=due_at,
                claimed_at=now,
                lease_fencing_token=fencing_token,
            ).sealed()
            self._transition(
                CampaignEventType.RUN_STARTED,
                CampaignState.RUNNING,
                now,
                {
                    "run_id": claim.run_id,
                    "run_index": claim.run_index,
                    "due_at": claim.due_at,
                    "claim_digest": claim.claim_digest,
                    "lease_fencing_token": fencing_token,
                },
            )
            self.active_run_id = claim.run_id
            self.active_run_index = claim.run_index
            return claim

    def record_evidence(
        self,
        evidence: QualificationRunEvidenceV102,
        *,
        now: datetime,
        owner_id: str,
        fencing_token: int,
    ) -> CampaignSnapshotV102:
        now = require_aware(now, field="now")
        with self._lock:
            self._assert_lease(owner_id, fencing_token, now)
            if self.state is not CampaignState.RUNNING or not self.active_run_id:
                raise SoakError("no active campaign run")
            try:
                evidence.validate()
            except (ValueError, SoakCorruption) as exc:
                return self._quarantine(now, (f"EVIDENCE_INVALID:{type(exc).__name__}",))
            reasons: list[str] = []
            if not evidence.evidence_digest:
                reasons.append("EVIDENCE_NOT_SEALED")
            if evidence.campaign_id != self.plan.campaign_id:
                reasons.append("CAMPAIGN_ID_MISMATCH")
            if evidence.run_id != self.active_run_id or evidence.run_index != self.active_run_index:
                reasons.append("RUN_IDENTITY_MISMATCH")
            if evidence.generation != self.plan.generation:
                reasons.append("GENERATION_MISMATCH")
            if evidence.captured_at > now:
                reasons.append("EVIDENCE_FROM_FUTURE")
            if now - evidence.captured_at > self.plan.evidence_max_age:
                reasons.append("EVIDENCE_STALE")
            if evidence.external_order_routing_allowed or evidence.live_trading_allowed:
                reasons.append("ROUTING_FLAGS_INVALID")
            security = {
                "EVIDENCE_NOT_SEALED",
                "CAMPAIGN_ID_MISMATCH",
                "RUN_IDENTITY_MISMATCH",
                "GENERATION_MISMATCH",
                "EVIDENCE_FROM_FUTURE",
                "ROUTING_FLAGS_INVALID",
            }
            if any(reason in security for reason in reasons):
                return self._quarantine(now, tuple(sorted(set(reasons))))
            manifest = self.evidence_archive.save(
                evidence,
                stored_at=now,
                retention=self.plan.evidence_retention,
            )
            if reasons:
                return self._record_failure(
                    evidence,
                    now,
                    tuple(sorted(set(reasons))),
                    manifest.record_digest,
                )
            if evidence.outcome is RunOutcome.VERIFIED_CLEAN:
                self._transition(
                    CampaignEventType.RUN_VERIFIED,
                    CampaignState.ACTIVE,
                    now,
                    {
                        "run_id": evidence.run_id,
                        "run_index": evidence.run_index,
                        "evidence_digest": evidence.evidence_digest,
                        "manifest_record_digest": manifest.record_digest,
                        "qualification_tail_digest": evidence.qualification_tail_digest,
                    },
                )
                self.completed_runs += 1
                self.verified_runs += 1
                self.consecutive_failures = 0
                self.active_run_id = ""
                self.active_run_index = 0
                self._finalize(now)
                return self.snapshot()
            if evidence.outcome is RunOutcome.QUARANTINED:
                return self._quarantine(
                    now,
                    tuple(sorted(set((*evidence.reasons, "UPSTREAM_QUARANTINED")))),
                    evidence=evidence,
                    manifest_digest=manifest.record_digest,
                )
            if evidence.outcome in {
                RunOutcome.RECOVERY_REQUIRED,
                RunOutcome.RESIDUAL_PAPER_EXPOSURE,
            } or evidence.kill_switch_engaged:
                return self._block(
                    now,
                    tuple(sorted(set((*evidence.reasons, evidence.outcome.value)))),
                    evidence=evidence,
                    manifest_digest=manifest.record_digest,
                )
            return self._record_failure(
                evidence,
                now,
                tuple(sorted(set(evidence.reasons or ("PREFLIGHT_BLOCKED",)))),
                manifest.record_digest,
            )

    def _record_failure(
        self,
        evidence: QualificationRunEvidenceV102,
        now: datetime,
        reasons: tuple[str, ...],
        manifest_digest: str,
    ) -> CampaignSnapshotV102:
        self._transition(
            CampaignEventType.RUN_FAILED,
            CampaignState.ACTIVE,
            now,
            {
                "run_id": evidence.run_id,
                "run_index": evidence.run_index,
                "outcome": evidence.outcome,
                "reasons": reasons,
                "evidence_digest": evidence.evidence_digest,
                "manifest_record_digest": manifest_digest,
            },
        )
        self.completed_runs += 1
        self.total_failures += 1
        self.consecutive_failures += 1
        self.active_run_id = ""
        self.active_run_index = 0
        self._enforce_budgets(now)
        self._finalize(now)
        return self.snapshot()

    def _enforce_budgets(self, now: datetime) -> None:
        reasons: list[str] = []
        if self.total_failures > self.plan.maximum_total_failures:
            reasons.append("TOTAL_FAILURE_BUDGET_EXHAUSTED")
        if self.consecutive_failures > self.plan.maximum_consecutive_failures:
            reasons.append("CONSECUTIVE_FAILURE_BUDGET_EXHAUSTED")
        if reasons and self.state is CampaignState.ACTIVE:
            self._block(now, tuple(reasons))

    def _finalize(self, now: datetime) -> None:
        if self.state is not CampaignState.ACTIVE or self.completed_runs < self.plan.maximum_runs:
            return
        if self.verified_runs >= self.plan.minimum_verified_runs:
            self._transition(
                CampaignEventType.CAMPAIGN_COMPLETED,
                CampaignState.COMPLETED,
                now,
                {
                    "completed_runs": self.completed_runs,
                    "verified_runs": self.verified_runs,
                    "total_failures": self.total_failures,
                },
            )
        else:
            self._block(now, ("MINIMUM_VERIFIED_RUNS_NOT_MET",))

    def _block(
        self,
        now: datetime,
        reasons: tuple[str, ...],
        *,
        evidence: QualificationRunEvidenceV102 | None = None,
        manifest_digest: str = "",
    ) -> CampaignSnapshotV102:
        if self.state in {CampaignState.BLOCKED, CampaignState.QUARANTINED, CampaignState.COMPLETED}:
            return self.snapshot()
        self._transition(
            CampaignEventType.CAMPAIGN_BLOCKED,
            CampaignState.BLOCKED,
            now,
            {
                "reasons": tuple(sorted(set(reasons))),
                "active_run_id": self.active_run_id,
                "evidence_digest": "" if evidence is None else evidence.evidence_digest,
                "manifest_record_digest": manifest_digest,
            },
        )
        return self.snapshot()

    def _quarantine(
        self,
        now: datetime,
        reasons: tuple[str, ...],
        *,
        evidence: QualificationRunEvidenceV102 | None = None,
        manifest_digest: str = "",
    ) -> CampaignSnapshotV102:
        if self.state is CampaignState.QUARANTINED:
            return self.snapshot()
        self._transition(
            CampaignEventType.CAMPAIGN_QUARANTINED,
            CampaignState.QUARANTINED,
            now,
            {
                "reasons": tuple(sorted(set(reasons))),
                "active_run_id": self.active_run_id,
                "evidence_digest": "" if evidence is None else evidence.evidence_digest,
                "manifest_record_digest": manifest_digest,
            },
        )
        return self.snapshot()

    def _transition(
        self,
        event_type: CampaignEventType,
        to_state: CampaignState,
        now: datetime,
        attributes: Mapping[str, object],
    ) -> CampaignEventV102:
        if to_state not in TRANSITIONS.get((self.state, event_type), set()):
            raise SoakError(
                f"invalid transition {self.state.value}/{event_type.value}->{to_state.value}"
            )
        event = self.event_store.append(
            campaign_id=self.plan.campaign_id,
            event_type=event_type,
            from_state=self.state,
            to_state=to_state,
            occurred_at=now,
            generation=self.plan.generation,
            attributes={
                **dict(attributes),
                "external_order_routing_allowed": False,
                "live_trading_allowed": False,
            },
        )
        self.events = (*self.events, event)
        self.state = to_state
        return event

    def _assert_lease(self, owner_id: str, fencing_token: int, now: datetime) -> LeaseRecordV102:
        return self.lease_store.assert_held(
            owner_id=owner_id,
            generation=self.plan.generation,
            fencing_token=fencing_token,
            now=now,
        )

    def _kill_switch_engaged(self) -> bool:
        status = self.kill_switch.status()
        return bool(getattr(status, "engaged", True))

    def _run_id(self, run_index: int) -> str:
        return f"{self.plan.campaign_id}-{run_index:04d}"

    def snapshot(self) -> CampaignSnapshotV102:
        tail = self.events[-1].event_digest if self.events else ZERO_DIGEST
        eligible = (
            self.state is CampaignState.COMPLETED
            and self.verified_runs >= self.plan.minimum_verified_runs
            and not self._kill_switch_engaged()
        )
        return CampaignSnapshotV102(
            campaign_id=self.plan.campaign_id,
            state=self.state,
            generation=self.plan.generation,
            completed_runs=self.completed_runs,
            verified_runs=self.verified_runs,
            total_failures=self.total_failures,
            consecutive_failures=self.consecutive_failures,
            active_run_id=self.active_run_id,
            tail_digest=tail,
            eligible_for_extended_paper_soak=eligible,
            external_order_routing_allowed=False,
            live_trading_allowed=False,
        )
