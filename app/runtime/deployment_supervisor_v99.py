from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
import os
from pathlib import Path
import threading

from app.runtime.platform_common_v90 import canonical_json, require_aware, sha256_digest

UTC = timezone.utc


class DeploymentSupervisorError(RuntimeError):
    pass


class DeploymentLeaseConflict(DeploymentSupervisorError):
    pass


class StaleDeploymentGeneration(DeploymentSupervisorError):
    pass


class DeploymentState(str, Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    DRAINING = "DRAINING"
    QUARANTINED = "QUARANTINED"


@dataclass(frozen=True)
class DeploymentPolicyV99:
    lease_ttl: timedelta = timedelta(seconds=45)
    heartbeat_timeout: timedelta = timedelta(seconds=60)
    drain_timeout: timedelta = timedelta(minutes=5)
    crash_window: timedelta = timedelta(minutes=15)
    maximum_crashes: int = 3

    def validate(self) -> None:
        for name, value in (
            ("lease_ttl", self.lease_ttl),
            ("heartbeat_timeout", self.heartbeat_timeout),
            ("drain_timeout", self.drain_timeout),
            ("crash_window", self.crash_window),
        ):
            if value <= timedelta(0):
                raise ValueError(f"{name} must be positive")
        if self.heartbeat_timeout < self.lease_ttl:
            raise ValueError("heartbeat_timeout must be at least lease_ttl")
        if self.maximum_crashes <= 0:
            raise ValueError("maximum_crashes must be positive")


@dataclass(frozen=True)
class DeploymentCheckpointV99:
    service_name: str
    state: DeploymentState
    instance_id: str
    generation: int
    updated_at: datetime
    lease_expires_at: datetime | None
    drain_deadline: datetime | None
    crash_times: tuple[datetime, ...]
    quarantine_reason: str
    digest: str = ""

    def validate(self) -> None:
        require_aware(self.updated_at, field_name="updated_at")
        if self.lease_expires_at is not None:
            require_aware(self.lease_expires_at, field_name="lease_expires_at")
        if self.drain_deadline is not None:
            require_aware(self.drain_deadline, field_name="drain_deadline")
        for index, value in enumerate(self.crash_times):
            require_aware(value, field_name=f"crash_times[{index}]")
        if not self.service_name.strip():
            raise ValueError("service_name is required")
        if self.state is not DeploymentState.STOPPED and not self.instance_id.strip():
            raise ValueError("active checkpoint requires instance_id")
        if self.generation < 0:
            raise ValueError("generation cannot be negative")
        if self.digest and self.digest != self.computed_digest():
            raise DeploymentSupervisorError("deployment checkpoint digest mismatch")

    def computed_digest(self) -> str:
        return sha256_digest(
            {
                "service_name": self.service_name,
                "state": self.state,
                "instance_id": self.instance_id,
                "generation": self.generation,
                "updated_at": self.updated_at,
                "lease_expires_at": self.lease_expires_at,
                "drain_deadline": self.drain_deadline,
                "crash_times": self.crash_times,
                "quarantine_reason": self.quarantine_reason,
            }
        )

    def sealed(self) -> "DeploymentCheckpointV99":
        unsigned = replace(self, digest="")
        unsigned.validate()
        return replace(unsigned, digest=unsigned.computed_digest())


class FileDeploymentCheckpointStoreV99:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def save(self, checkpoint: DeploymentCheckpointV99) -> DeploymentCheckpointV99:
        sealed = checkpoint.sealed()
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f".{self.path.name}.tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(canonical_json(sealed) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return sealed

    def load(self) -> DeploymentCheckpointV99 | None:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            checkpoint = DeploymentCheckpointV99(
                service_name=str(raw["service_name"]),
                state=DeploymentState(str(raw["state"])),
                instance_id=str(raw["instance_id"]),
                generation=int(raw["generation"]),
                updated_at=datetime.fromisoformat(str(raw["updated_at"]).replace("Z", "+00:00")),
                lease_expires_at=None
                if raw.get("lease_expires_at") is None
                else datetime.fromisoformat(str(raw["lease_expires_at"]).replace("Z", "+00:00")),
                drain_deadline=None
                if raw.get("drain_deadline") is None
                else datetime.fromisoformat(str(raw["drain_deadline"]).replace("Z", "+00:00")),
                crash_times=tuple(
                    datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                    for value in raw.get("crash_times", ())
                ),
                quarantine_reason=str(raw.get("quarantine_reason", "")),
                digest=str(raw["digest"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise DeploymentSupervisorError("invalid deployment checkpoint") from exc
        checkpoint.validate()
        return checkpoint


class DeploymentSupervisorV99:
    """Generation-fenced lease and crash-budget controller for one service."""

    def __init__(
        self,
        *,
        service_name: str,
        store: FileDeploymentCheckpointStoreV99,
        policy: DeploymentPolicyV99 = DeploymentPolicyV99(),
    ) -> None:
        if not service_name.strip():
            raise ValueError("service_name is required")
        policy.validate()
        self.service_name = service_name
        self.store = store
        self.policy = policy
        self._lock = threading.RLock()

    def acquire(self, *, instance_id: str, now: datetime) -> DeploymentCheckpointV99:
        now = require_aware(now, field_name="now")
        if not instance_id.strip():
            raise ValueError("instance_id is required")
        with self._lock:
            current = self.store.load()
            if current is not None and current.state is DeploymentState.QUARANTINED:
                raise DeploymentLeaseConflict("deployment is quarantined")
            if (
                current is not None
                and current.state in {DeploymentState.STARTING, DeploymentState.RUNNING, DeploymentState.DRAINING}
                and current.lease_expires_at is not None
                and current.lease_expires_at > now
                and current.instance_id != instance_id
            ):
                raise DeploymentLeaseConflict("another instance owns the active lease")
            generation = 1 if current is None else current.generation + 1
            crashes = () if current is None else self._recent_crashes(current, now=now)
            checkpoint = DeploymentCheckpointV99(
                service_name=self.service_name,
                state=DeploymentState.STARTING,
                instance_id=instance_id,
                generation=generation,
                updated_at=now,
                lease_expires_at=now + self.policy.lease_ttl,
                drain_deadline=None,
                crash_times=crashes,
                quarantine_reason="",
            )
            return self.store.save(checkpoint)

    def mark_running(
        self, *, instance_id: str, generation: int, now: datetime
    ) -> DeploymentCheckpointV99:
        return self._renew(
            instance_id=instance_id,
            generation=generation,
            now=now,
            state=DeploymentState.RUNNING,
        )

    def heartbeat(
        self, *, instance_id: str, generation: int, now: datetime
    ) -> DeploymentCheckpointV99:
        current = self._require_owner(instance_id=instance_id, generation=generation, now=now)
        if current.state not in {DeploymentState.STARTING, DeploymentState.RUNNING, DeploymentState.DRAINING}:
            raise DeploymentSupervisorError("heartbeat is not allowed in current state")
        return self._renew(
            instance_id=instance_id,
            generation=generation,
            now=now,
            state=current.state,
        )

    def request_drain(
        self, *, instance_id: str, generation: int, now: datetime
    ) -> DeploymentCheckpointV99:
        now = require_aware(now, field_name="now")
        current = self._require_owner(instance_id=instance_id, generation=generation, now=now)
        if current.state not in {DeploymentState.STARTING, DeploymentState.RUNNING}:
            raise DeploymentSupervisorError("drain requires STARTING or RUNNING state")
        return self.store.save(
            replace(
                current,
                state=DeploymentState.DRAINING,
                updated_at=now,
                lease_expires_at=now + self.policy.lease_ttl,
                drain_deadline=now + self.policy.drain_timeout,
                digest="",
            )
        )

    def stop(
        self,
        *,
        instance_id: str,
        generation: int,
        now: datetime,
        outstanding_work: int,
    ) -> DeploymentCheckpointV99:
        now = require_aware(now, field_name="now")
        current = self._require_owner(instance_id=instance_id, generation=generation, now=now)
        if current.state is not DeploymentState.DRAINING:
            raise DeploymentSupervisorError("stop requires DRAINING state")
        if outstanding_work < 0:
            raise ValueError("outstanding_work cannot be negative")
        if outstanding_work:
            if current.drain_deadline is not None and now >= current.drain_deadline:
                return self._quarantine(current, now=now, reason="DRAIN_TIMEOUT_WITH_OUTSTANDING_WORK")
            raise DeploymentSupervisorError("outstanding work remains")
        return self.store.save(
            replace(
                current,
                state=DeploymentState.STOPPED,
                instance_id="",
                updated_at=now,
                lease_expires_at=None,
                drain_deadline=None,
                digest="",
            )
        )

    def record_crash(
        self, *, instance_id: str, generation: int, now: datetime, reason: str
    ) -> DeploymentCheckpointV99:
        now = require_aware(now, field_name="now")
        current = self._require_owner(instance_id=instance_id, generation=generation, now=now)
        crashes = (*self._recent_crashes(current, now=now), now)
        updated = replace(
            current,
            crash_times=crashes,
            updated_at=now,
            lease_expires_at=None,
            digest="",
        )
        if len(crashes) >= self.policy.maximum_crashes:
            return self._quarantine(updated, now=now, reason=f"CRASH_BUDGET_EXCEEDED:{reason}")
        return self.store.save(
            replace(
                updated,
                state=DeploymentState.STOPPED,
                instance_id="",
                drain_deadline=None,
                digest="",
            )
        )

    def inspect(self, *, now: datetime) -> DeploymentCheckpointV99 | None:
        now = require_aware(now, field_name="now")
        current = self.store.load()
        if current is None:
            return None
        if (
            current.state in {DeploymentState.STARTING, DeploymentState.RUNNING, DeploymentState.DRAINING}
            and current.lease_expires_at is not None
            and now - current.updated_at > self.policy.heartbeat_timeout
        ):
            return self._quarantine(current, now=now, reason="HEARTBEAT_TIMEOUT")
        return current

    def _renew(
        self,
        *,
        instance_id: str,
        generation: int,
        now: datetime,
        state: DeploymentState,
    ) -> DeploymentCheckpointV99:
        now = require_aware(now, field_name="now")
        current = self._require_owner(instance_id=instance_id, generation=generation, now=now)
        return self.store.save(
            replace(
                current,
                state=state,
                updated_at=now,
                lease_expires_at=now + self.policy.lease_ttl,
                digest="",
            )
        )

    def _require_owner(
        self, *, instance_id: str, generation: int, now: datetime
    ) -> DeploymentCheckpointV99:
        now = require_aware(now, field_name="now")
        current = self.store.load()
        if current is None:
            raise DeploymentSupervisorError("deployment checkpoint is absent")
        if generation != current.generation:
            raise StaleDeploymentGeneration("deployment generation is stale")
        if instance_id != current.instance_id:
            raise DeploymentLeaseConflict("deployment lease owner mismatch")
        if current.lease_expires_at is not None and current.lease_expires_at <= now:
            raise DeploymentLeaseConflict("deployment lease expired")
        return current

    def _recent_crashes(
        self, checkpoint: DeploymentCheckpointV99, *, now: datetime
    ) -> tuple[datetime, ...]:
        threshold = now - self.policy.crash_window
        return tuple(value for value in checkpoint.crash_times if value >= threshold)

    def _quarantine(
        self, checkpoint: DeploymentCheckpointV99, *, now: datetime, reason: str
    ) -> DeploymentCheckpointV99:
        return self.store.save(
            replace(
                checkpoint,
                state=DeploymentState.QUARANTINED,
                updated_at=now,
                lease_expires_at=None,
                drain_deadline=None,
                quarantine_reason=reason,
                digest="",
            )
        )
