from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import hmac
import json
import re
import threading
from typing import Any, Iterable, Mapping, Sequence

UTC = timezone.utc
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")


class QualificationErrorV106(RuntimeError):
    pass


class ValidationErrorV106(QualificationErrorV106):
    pass


class SignatureErrorV106(QualificationErrorV106):
    pass


class ReplayErrorV106(QualificationErrorV106):
    pass


class StateTransitionErrorV106(QualificationErrorV106):
    pass


class GateSeverityV106(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class QualificationStateV106(str, Enum):
    PLANNED = "PLANNED"
    PREFLIGHT = "PREFLIGHT"
    CANARY = "CANARY"
    OBSERVING = "OBSERVING"
    PROMOTABLE = "PROMOTABLE"
    PROMOTION_PENDING = "PROMOTION_PENDING"
    PROMOTED = "PROMOTED"
    ROLLBACK_PENDING = "ROLLBACK_PENDING"
    ROLLED_BACK = "ROLLED_BACK"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    QUARANTINED = "QUARANTINED"


class RolloutActionTypeV106(str, Enum):
    PROMOTE = "PROMOTE"
    ROLLBACK = "ROLLBACK"


class RolloutActionStatusV106(str, Enum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    ACKED = "ACKED"
    FAILED = "FAILED"


class CertificateDrillStateV106(str, Enum):
    PLANNED = "PLANNED"
    ISSUED = "ISSUED"
    ACTIVATED = "ACTIVATED"
    OLD_REVOKED = "OLD_REVOKED"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"


class DisasterRecoveryStateV106(str, Enum):
    PLANNED = "PLANNED"
    RESTORING = "RESTORING"
    VERIFYING = "VERIFYING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    QUARANTINED = "QUARANTINED"


def _canonical(value: Any) -> bytes:
    def default(obj: Any) -> Any:
        if isinstance(obj, datetime):
            return _ensure_utc(obj).isoformat().replace("+00:00", "Z")
        if isinstance(obj, Enum):
            return obj.value
        if hasattr(obj, "to_payload"):
            return obj.to_payload()
        if hasattr(obj, "__dataclass_fields__"):
            return asdict(obj)
        raise TypeError(type(obj).__name__)

    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=default).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationErrorV106("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _validate_id(value: str, name: str) -> None:
    if not _ID_RE.fullmatch(value):
        raise ValidationErrorV106(f"invalid {name}")


def _validate_hex(value: str, name: str) -> None:
    if not _HEX64_RE.fullmatch(value):
        raise ValidationErrorV106(f"invalid {name}")


def _validate_sha256(value: str, name: str) -> None:
    if not _SHA256_RE.fullmatch(value):
        raise ValidationErrorV106(f"invalid {name}")


def _validate_host(value: str, name: str) -> None:
    if not _HOST_RE.fullmatch(value) or value.startswith(".") or ".." in value:
        raise ValidationErrorV106(f"invalid {name}")


def _require_distinct_approvers(approver_a: str, approver_b: str) -> None:
    _validate_id(approver_a, "approver_a")
    _validate_id(approver_b, "approver_b")
    if approver_a == approver_b:
        raise ValidationErrorV106("dual control requires distinct approvers")


@dataclass(frozen=True, slots=True)
class DeploymentPolicyV106:
    fleet_id: str
    environment: str
    expected_cluster: str
    namespace: str
    service_account: str
    expected_image_digest: str
    expected_config_digest: str
    allowed_kubernetes_hosts: tuple[str, ...]
    allowed_s3_hosts: tuple[str, ...]
    paper_broker_host: str = "paper-api.alpaca.markets"
    live_broker_host: str = "api.alpaca.markets"
    min_replicas: int = 2
    max_replicas: int = 20
    canary_replicas: int = 1
    min_ready_replicas: int = 2
    max_unavailable: int = 1
    max_error_rate_bps: int = 50
    max_p95_latency_ms: int = 1_500
    min_observation_samples: int = 5
    min_observation_seconds: int = 300
    heartbeat_max_age_seconds: int = 90
    cert_min_remaining_seconds: int = 3_600
    cert_max_overlap_seconds: int = 900
    backup_max_age_seconds: int = 86_400
    max_rpo_seconds: int = 900
    max_rto_seconds: int = 1_800
    max_clock_skew_seconds: int = 5
    max_restart_count: int = 2
    max_dlq_depth: int = 0
    max_open_incidents: int = 0
    max_failure_samples: int = 0
    required_zones: tuple[str, ...] = ("zone-a", "zone-b")
    required_egress: tuple[str, ...] = (
        "dns:53/tcp",
        "dns:53/udp",
        "postgresql:5432/tcp",
        "paper-api.alpaca.markets:443/tcp",
    )
    external_order_routing_allowed: bool = False
    live_trading_allowed: bool = False

    def __post_init__(self) -> None:
        for name in ("fleet_id", "environment", "expected_cluster", "namespace", "service_account"):
            _validate_id(getattr(self, name), name)
        _validate_sha256(self.expected_image_digest, "expected_image_digest")
        _validate_sha256(self.expected_config_digest, "expected_config_digest")
        if not self.allowed_kubernetes_hosts or not self.allowed_s3_hosts:
            raise ValidationErrorV106("host allowlists must not be empty")
        for host in (*self.allowed_kubernetes_hosts, *self.allowed_s3_hosts, self.paper_broker_host, self.live_broker_host):
            _validate_host(host, "host")
        if self.paper_broker_host == self.live_broker_host:
            raise ValidationErrorV106("paper and live broker hosts must differ")
        if self.live_broker_host in self.allowed_kubernetes_hosts or self.live_broker_host in self.allowed_s3_hosts:
            raise ValidationErrorV106("live broker host must not be allowlisted")
        if self.min_replicas < 1 or self.max_replicas < self.min_replicas:
            raise ValidationErrorV106("invalid replica bounds")
        if not (1 <= self.canary_replicas <= self.min_replicas):
            raise ValidationErrorV106("invalid canary replicas")
        if not (1 <= self.min_ready_replicas <= self.max_replicas):
            raise ValidationErrorV106("invalid min ready replicas")
        if not (0 <= self.max_unavailable < self.max_replicas):
            raise ValidationErrorV106("invalid max unavailable")
        if not (0 <= self.max_error_rate_bps <= 10_000):
            raise ValidationErrorV106("invalid error rate")
        for name in (
            "max_p95_latency_ms",
            "min_observation_samples",
            "min_observation_seconds",
            "heartbeat_max_age_seconds",
            "cert_min_remaining_seconds",
            "cert_max_overlap_seconds",
            "backup_max_age_seconds",
            "max_rpo_seconds",
            "max_rto_seconds",
            "max_clock_skew_seconds",
        ):
            if getattr(self, name) <= 0:
                raise ValidationErrorV106(f"{name} must be positive")
        for name in ("max_restart_count", "max_dlq_depth", "max_open_incidents", "max_failure_samples"):
            if getattr(self, name) < 0:
                raise ValidationErrorV106(f"{name} must be non-negative")
        if not self.required_zones or len(set(self.required_zones)) != len(self.required_zones):
            raise ValidationErrorV106("required zones must be non-empty and unique")
        for zone in self.required_zones:
            _validate_id(zone, "zone")
        if not self.required_egress or len(set(self.required_egress)) != len(self.required_egress):
            raise ValidationErrorV106("required egress must be non-empty and unique")
        if any(entry.split(":", 1)[0] == self.live_broker_host or "0.0.0.0/0" in entry or "::/0" in entry for entry in self.required_egress):
            raise ValidationErrorV106("unsafe required egress")
        if self.external_order_routing_allowed or self.live_trading_allowed:
            raise ValidationErrorV106("production qualification must remain read-only")

    @property
    def policy_digest(self) -> str:
        return _digest(asdict(self))


@dataclass(frozen=True, slots=True)
class SignedDeploymentManifestV106:
    manifest_id: str
    rollout_id: str
    deployment_id: str
    fleet_id: str
    environment: str
    generation: int
    image_digest: str
    config_digest: str
    replicas: int
    canary_replicas: int
    issued_at: datetime
    not_before: datetime
    expires_at: datetime
    nonce: str
    key_id: str
    signature: str

    def __post_init__(self) -> None:
        for name in ("manifest_id", "rollout_id", "deployment_id", "fleet_id", "environment", "nonce", "key_id"):
            _validate_id(getattr(self, name), name)
        _validate_sha256(self.image_digest, "image_digest")
        _validate_sha256(self.config_digest, "config_digest")
        _validate_hex(self.signature, "signature")
        if self.generation <= 0 or self.replicas <= 0 or self.canary_replicas <= 0:
            raise ValidationErrorV106("manifest numeric fields must be positive")
        issued = _ensure_utc(self.issued_at)
        not_before = _ensure_utc(self.not_before)
        expires = _ensure_utc(self.expires_at)
        if not (issued <= not_before < expires):
            raise ValidationErrorV106("invalid manifest validity interval")

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("signature")
        return payload

    @property
    def manifest_digest(self) -> str:
        return _digest(self.to_payload())

    @classmethod
    def sign(
        cls,
        *,
        manifest_id: str,
        rollout_id: str,
        deployment_id: str,
        fleet_id: str,
        environment: str,
        generation: int,
        image_digest: str,
        config_digest: str,
        replicas: int,
        canary_replicas: int,
        issued_at: datetime,
        not_before: datetime,
        expires_at: datetime,
        nonce: str,
        key_id: str,
        secret: bytes,
    ) -> "SignedDeploymentManifestV106":
        if len(secret) < 32:
            raise ValidationErrorV106("signing secret must be at least 32 bytes")
        unsigned = cls(
            manifest_id=manifest_id,
            rollout_id=rollout_id,
            deployment_id=deployment_id,
            fleet_id=fleet_id,
            environment=environment,
            generation=generation,
            image_digest=image_digest,
            config_digest=config_digest,
            replicas=replicas,
            canary_replicas=canary_replicas,
            issued_at=issued_at,
            not_before=not_before,
            expires_at=expires_at,
            nonce=nonce,
            key_id=key_id,
            signature="0" * 64,
        )
        signature = hmac.new(secret, _canonical(unsigned.to_payload()), hashlib.sha256).hexdigest()
        return replace(unsigned, signature=signature)

    def verify(
        self,
        *,
        policy: DeploymentPolicyV106,
        keyring: Mapping[str, bytes],
        now: datetime,
        replay_ledger: "ManifestReplayLedgerV106 | None" = None,
    ) -> None:
        current = _ensure_utc(now)
        secret = keyring.get(self.key_id)
        if secret is None or len(secret) < 32:
            raise SignatureErrorV106("unknown or weak signing key")
        expected = hmac.new(secret, _canonical(self.to_payload()), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, self.signature):
            raise SignatureErrorV106("manifest signature mismatch")
        if current < _ensure_utc(self.not_before) or current > _ensure_utc(self.expires_at):
            raise SignatureErrorV106("manifest outside validity interval")
        if self.fleet_id != policy.fleet_id or self.environment != policy.environment:
            raise ValidationErrorV106("manifest scope mismatch")
        if self.image_digest != policy.expected_image_digest or self.config_digest != policy.expected_config_digest:
            raise ValidationErrorV106("manifest release identity mismatch")
        if not (policy.min_replicas <= self.replicas <= policy.max_replicas):
            raise ValidationErrorV106("manifest replicas outside policy")
        if self.canary_replicas != policy.canary_replicas:
            raise ValidationErrorV106("manifest canary replicas mismatch")
        if replay_ledger is not None:
            replay_ledger.consume(self.manifest_id, self.nonce, current)


class ManifestReplayLedgerV106:
    def __init__(self) -> None:
        self._manifest_ids: set[str] = set()
        self._nonces: set[str] = set()
        self._lock = threading.Lock()

    def consume(self, manifest_id: str, nonce: str, observed_at: datetime) -> None:
        _validate_id(manifest_id, "manifest_id")
        _validate_id(nonce, "nonce")
        _ensure_utc(observed_at)
        with self._lock:
            if manifest_id in self._manifest_ids:
                raise ReplayErrorV106("manifest already consumed")
            if nonce in self._nonces:
                raise ReplayErrorV106("manifest nonce already consumed")
            self._manifest_ids.add(manifest_id)
            self._nonces.add(nonce)

    def __len__(self) -> int:
        with self._lock:
            return len(self._manifest_ids)


@dataclass(frozen=True, slots=True)
class PodSnapshotV106:
    pod_uid: str
    worker_id: str
    zone: str
    image_digest: str
    config_digest: str
    ready: bool
    is_canary: bool
    restart_count: int
    heartbeat_at: datetime
    certificate_not_after: datetime
    active_claims: int
    evidence_pending: int
    broker_mutation_count: int

    def __post_init__(self) -> None:
        for name in ("pod_uid", "worker_id", "zone"):
            _validate_id(getattr(self, name), name)
        _validate_sha256(self.image_digest, "image_digest")
        _validate_sha256(self.config_digest, "config_digest")
        _ensure_utc(self.heartbeat_at)
        _ensure_utc(self.certificate_not_after)
        for name in ("restart_count", "active_claims", "evidence_pending", "broker_mutation_count"):
            if getattr(self, name) < 0:
                raise ValidationErrorV106(f"{name} must be non-negative")

    @property
    def digest(self) -> str:
        return _digest(asdict(self))


@dataclass(frozen=True, slots=True)
class NetworkPolicySnapshotV106:
    default_deny_ingress: bool
    default_deny_egress: bool
    allowed_egress: tuple[str, ...]
    broad_cidrs: tuple[str, ...] = ()
    live_hosts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(set(self.allowed_egress)) != len(self.allowed_egress):
            raise ValidationErrorV106("duplicate egress entries")
        for host in self.live_hosts:
            _validate_host(host, "live_host")

    @property
    def digest(self) -> str:
        return _digest(asdict(self))


@dataclass(frozen=True, slots=True)
class DisruptionBudgetSnapshotV106:
    min_available: int | None
    max_unavailable: int | None
    unhealthy_pod_eviction_policy: str

    def __post_init__(self) -> None:
        if self.min_available is None and self.max_unavailable is None:
            raise ValidationErrorV106("PDB must specify min_available or max_unavailable")
        if self.min_available is not None and self.min_available < 0:
            raise ValidationErrorV106("invalid min_available")
        if self.max_unavailable is not None and self.max_unavailable < 0:
            raise ValidationErrorV106("invalid max_unavailable")
        if self.unhealthy_pod_eviction_policy not in {"IfHealthyBudget", "AlwaysAllow"}:
            raise ValidationErrorV106("invalid unhealthy pod eviction policy")

    @property
    def digest(self) -> str:
        return _digest(asdict(self))


@dataclass(frozen=True, slots=True)
class KubernetesDeploymentSnapshotV106:
    cluster: str
    namespace: str
    service_account: str
    deployment_id: str
    generation: int
    observed_at: datetime
    desired_replicas: int
    available_replicas: int
    canary_ready_replicas: int
    zone_replicas: tuple[tuple[str, int], ...]
    pods: tuple[PodSnapshotV106, ...]
    network_policy: NetworkPolicySnapshotV106
    disruption_budget: DisruptionBudgetSnapshotV106
    external_order_routing_allowed: bool = False
    live_trading_allowed: bool = False

    def __post_init__(self) -> None:
        for name in ("cluster", "namespace", "service_account", "deployment_id"):
            _validate_id(getattr(self, name), name)
        _ensure_utc(self.observed_at)
        if self.generation <= 0:
            raise ValidationErrorV106("generation must be positive")
        for name in ("desired_replicas", "available_replicas", "canary_ready_replicas"):
            if getattr(self, name) < 0:
                raise ValidationErrorV106(f"{name} must be non-negative")
        if self.available_replicas > self.desired_replicas:
            raise ValidationErrorV106("available replicas exceed desired")
        if len({pod.pod_uid for pod in self.pods}) != len(self.pods):
            raise ValidationErrorV106("duplicate pod uid")
        if len({pod.worker_id for pod in self.pods}) != len(self.pods):
            raise ValidationErrorV106("duplicate worker id")
        zone_names = [zone for zone, count in self.zone_replicas]
        if len(set(zone_names)) != len(zone_names) or any(count < 0 for _, count in self.zone_replicas):
            raise ValidationErrorV106("invalid zone replica map")

    @property
    def digest(self) -> str:
        return _digest(asdict(self))


@dataclass(frozen=True, slots=True)
class DependencySnapshotV106:
    observed_at: datetime
    postgres_ready: bool
    object_storage_ready: bool
    control_plane_ready: bool
    identity_authority_ready: bool
    clock_offset_seconds: float
    backup_age_seconds: int
    postgres_evidence_digest: str
    object_storage_evidence_digest: str

    def __post_init__(self) -> None:
        _ensure_utc(self.observed_at)
        if self.backup_age_seconds < 0:
            raise ValidationErrorV106("backup age must be non-negative")
        _validate_hex(self.postgres_evidence_digest, "postgres_evidence_digest")
        _validate_hex(self.object_storage_evidence_digest, "object_storage_evidence_digest")

    @property
    def digest(self) -> str:
        return _digest(asdict(self))


@dataclass(frozen=True, slots=True)
class GateEvaluationV106:
    name: str
    passed: bool
    severity: GateSeverityV106
    reason: str
    evidence_digest: str

    def __post_init__(self) -> None:
        _validate_id(self.name, "gate name")
        if not self.reason or len(self.reason) > 512:
            raise ValidationErrorV106("invalid gate reason")
        _validate_hex(self.evidence_digest, "evidence_digest")

    @property
    def digest(self) -> str:
        return _digest(asdict(self))


@dataclass(frozen=True, slots=True)
class GateSetV106:
    gates: tuple[GateEvaluationV106, ...]

    def __post_init__(self) -> None:
        if not self.gates:
            raise ValidationErrorV106("gate set must not be empty")
        names = [gate.name for gate in self.gates]
        if len(set(names)) != len(names):
            raise ValidationErrorV106("duplicate gate names")

    @property
    def passed(self) -> bool:
        return all(gate.passed for gate in self.gates if gate.severity == GateSeverityV106.CRITICAL)

    @property
    def critical_failures(self) -> tuple[GateEvaluationV106, ...]:
        return tuple(gate for gate in self.gates if not gate.passed and gate.severity == GateSeverityV106.CRITICAL)

    @property
    def digest(self) -> str:
        return _digest([asdict(gate) for gate in self.gates])


def evaluate_preflight_v106(
    *,
    policy: DeploymentPolicyV106,
    manifest: SignedDeploymentManifestV106,
    snapshot: KubernetesDeploymentSnapshotV106,
    dependencies: DependencySnapshotV106,
    now: datetime,
) -> GateSetV106:
    current = _ensure_utc(now)
    evidence = _digest({"snapshot": snapshot.digest, "dependencies": dependencies.digest, "manifest": manifest.manifest_digest})
    zones = dict(snapshot.zone_replicas)
    canary_pods = tuple(pod for pod in snapshot.pods if pod.is_canary)
    required_egress = set(policy.required_egress) | {f"{host}:443/tcp" for host in policy.allowed_s3_hosts}
    gates: list[GateEvaluationV106] = []

    def add(name: str, passed: bool, reason: str, severity: GateSeverityV106 = GateSeverityV106.CRITICAL) -> None:
        gates.append(GateEvaluationV106(name, passed, severity, reason, evidence))

    add("scope", snapshot.cluster == policy.expected_cluster and snapshot.namespace == policy.namespace and snapshot.service_account == policy.service_account and snapshot.deployment_id == manifest.deployment_id, "cluster/namespace/service-account/deployment scope")
    add("generation", snapshot.generation == manifest.generation, "deployment generation matches signed manifest")
    add("replica_window", manifest.canary_replicas <= snapshot.desired_replicas <= manifest.replicas, "desired replicas are between canary and target")
    add("canary_ready", snapshot.canary_ready_replicas >= policy.canary_replicas and len(canary_pods) >= policy.canary_replicas and all(pod.ready for pod in canary_pods), "required canary pods are ready")
    add("release_identity", bool(snapshot.pods) and all(pod.image_digest == manifest.image_digest and pod.config_digest == manifest.config_digest for pod in snapshot.pods), "all pods match signed image and configuration digests")
    add("restart_budget", all(pod.restart_count <= policy.max_restart_count for pod in snapshot.pods), "pod restart counts remain within policy")
    add("heartbeat_freshness", all((current - _ensure_utc(pod.heartbeat_at)).total_seconds() <= policy.heartbeat_max_age_seconds for pod in snapshot.pods), "all worker heartbeats are fresh")
    add("certificate_lifetime", all((_ensure_utc(pod.certificate_not_after) - current).total_seconds() >= policy.cert_min_remaining_seconds for pod in snapshot.pods), "worker certificates have sufficient remaining lifetime")
    add("claim_isolation", all(pod.active_claims == 0 for pod in canary_pods), "canary pods have no active claims before observation")
    add("evidence_spool", all(pod.evidence_pending == 0 for pod in canary_pods), "canary evidence spools are empty before observation")
    add("broker_mutations", all(pod.broker_mutation_count == 0 for pod in snapshot.pods), "no broker mutation was observed")
    add("routing_boundary", not snapshot.external_order_routing_allowed and not snapshot.live_trading_allowed, "external order routing and live trading remain disabled")
    add("network_default_deny", snapshot.network_policy.default_deny_ingress and snapshot.network_policy.default_deny_egress, "network policy defaults deny ingress and egress")
    add("network_allowlist", set(snapshot.network_policy.allowed_egress) == required_egress, "egress allowlist exactly matches policy")
    add("network_no_broad_cidr", not snapshot.network_policy.broad_cidrs, "network policy contains no broad CIDR")
    add("network_no_live_host", policy.live_broker_host not in snapshot.network_policy.live_hosts and all(entry.split(":", 1)[0] != policy.live_broker_host for entry in snapshot.network_policy.allowed_egress), "live broker endpoint is absent")
    pdb_ok = (snapshot.disruption_budget.min_available is not None and snapshot.disruption_budget.min_available >= policy.min_ready_replicas) or (snapshot.disruption_budget.max_unavailable is not None and snapshot.disruption_budget.max_unavailable <= policy.max_unavailable)
    add("disruption_budget", pdb_ok, "pod disruption budget preserves minimum availability")
    add("zone_spread", all(zones.get(zone, 0) > 0 for zone in policy.required_zones), "required availability zones contain workers")
    add("postgres_ready", dependencies.postgres_ready, "PostgreSQL dependency is ready")
    add("object_storage_ready", dependencies.object_storage_ready, "object storage dependency is ready")
    add("control_plane_ready", dependencies.control_plane_ready, "control plane dependency is ready")
    add("identity_authority_ready", dependencies.identity_authority_ready, "identity authority dependency is ready")
    add("clock_skew", abs(dependencies.clock_offset_seconds) <= policy.max_clock_skew_seconds, "clock offset remains within policy")
    add("backup_freshness", dependencies.backup_age_seconds <= policy.backup_max_age_seconds, "latest backup remains within freshness policy")
    add("snapshot_freshness", abs((current - _ensure_utc(snapshot.observed_at)).total_seconds()) <= policy.heartbeat_max_age_seconds and abs((current - _ensure_utc(dependencies.observed_at)).total_seconds()) <= policy.heartbeat_max_age_seconds, "deployment and dependency snapshots are fresh")
    return GateSetV106(tuple(gates))


@dataclass(frozen=True, slots=True)
class ObservationSampleV106:
    sample_id: str
    observed_at: datetime
    ready_replicas: int
    canary_ready_replicas: int
    request_count: int
    error_count: int
    p95_latency_ms: int
    stale_heartbeats: int
    crashloops: int
    dlq_depth: int
    open_incidents: int
    broker_mutation_count: int
    external_order_routing_allowed: bool
    live_trading_allowed: bool
    evidence_digest: str

    def __post_init__(self) -> None:
        _validate_id(self.sample_id, "sample_id")
        _ensure_utc(self.observed_at)
        for name in (
            "ready_replicas",
            "canary_ready_replicas",
            "request_count",
            "error_count",
            "p95_latency_ms",
            "stale_heartbeats",
            "crashloops",
            "dlq_depth",
            "open_incidents",
            "broker_mutation_count",
        ):
            if getattr(self, name) < 0:
                raise ValidationErrorV106(f"{name} must be non-negative")
        if self.error_count > self.request_count:
            raise ValidationErrorV106("error count exceeds request count")
        _validate_hex(self.evidence_digest, "evidence_digest")

    @property
    def error_rate_bps(self) -> int:
        if self.request_count == 0:
            return 0 if self.error_count == 0 else 10_000
        return (self.error_count * 10_000) // self.request_count

    @property
    def digest(self) -> str:
        return _digest(asdict(self))


def evaluate_observation_v106(policy: DeploymentPolicyV106, sample: ObservationSampleV106) -> GateSetV106:
    evidence = sample.evidence_digest
    gates = (
        GateEvaluationV106("ready_replicas", sample.ready_replicas >= policy.canary_replicas, GateSeverityV106.CRITICAL, "canary has ready capacity", evidence),
        GateEvaluationV106("canary_ready", sample.canary_ready_replicas >= policy.canary_replicas, GateSeverityV106.CRITICAL, "all canary replicas are ready", evidence),
        GateEvaluationV106("error_rate", sample.error_rate_bps <= policy.max_error_rate_bps, GateSeverityV106.CRITICAL, "error rate remains within policy", evidence),
        GateEvaluationV106("latency", sample.p95_latency_ms <= policy.max_p95_latency_ms, GateSeverityV106.CRITICAL, "p95 latency remains within policy", evidence),
        GateEvaluationV106("heartbeats", sample.stale_heartbeats == 0, GateSeverityV106.CRITICAL, "no stale worker heartbeat", evidence),
        GateEvaluationV106("crashloops", sample.crashloops == 0, GateSeverityV106.CRITICAL, "no crashlooping worker", evidence),
        GateEvaluationV106("dlq", sample.dlq_depth <= policy.max_dlq_depth, GateSeverityV106.CRITICAL, "DLQ depth remains within policy", evidence),
        GateEvaluationV106("incidents", sample.open_incidents <= policy.max_open_incidents, GateSeverityV106.CRITICAL, "open incidents remain within policy", evidence),
        GateEvaluationV106("broker_mutations", sample.broker_mutation_count == 0, GateSeverityV106.CRITICAL, "no broker mutation occurred", evidence),
        GateEvaluationV106("routing_boundary", not sample.external_order_routing_allowed and not sample.live_trading_allowed, GateSeverityV106.CRITICAL, "external routing and live trading remain disabled", evidence),
        GateEvaluationV106("traffic_observed", sample.request_count > 0, GateSeverityV106.WARNING, "read-only canary traffic was observed", evidence),
    )
    return GateSetV106(gates)


@dataclass(frozen=True, slots=True)
class JournalEventV106:
    sequence: int
    event_type: str
    observed_at: datetime
    payload_digest: str
    previous_digest: str
    event_digest: str

    def __post_init__(self) -> None:
        if self.sequence <= 0:
            raise ValidationErrorV106("event sequence must be positive")
        _validate_id(self.event_type, "event_type")
        _ensure_utc(self.observed_at)
        for name in ("payload_digest", "previous_digest", "event_digest"):
            _validate_hex(getattr(self, name), name)


class QualificationJournalV106:
    GENESIS = "0" * 64

    def __init__(self) -> None:
        self._events: list[JournalEventV106] = []
        self._lock = threading.Lock()

    def append(self, event_type: str, payload: Any, observed_at: datetime) -> JournalEventV106:
        _validate_id(event_type, "event_type")
        current = _ensure_utc(observed_at)
        payload_digest = _digest(payload)
        with self._lock:
            previous = self._events[-1].event_digest if self._events else self.GENESIS
            sequence = len(self._events) + 1
            event_digest = _digest({
                "sequence": sequence,
                "event_type": event_type,
                "observed_at": current,
                "payload_digest": payload_digest,
                "previous_digest": previous,
            })
            event = JournalEventV106(sequence, event_type, current, payload_digest, previous, event_digest)
            self._events.append(event)
            return event

    def verify(self) -> bool:
        previous = self.GENESIS
        for expected_sequence, event in enumerate(self._events, start=1):
            if event.sequence != expected_sequence or event.previous_digest != previous:
                return False
            expected = _digest({
                "sequence": event.sequence,
                "event_type": event.event_type,
                "observed_at": event.observed_at,
                "payload_digest": event.payload_digest,
                "previous_digest": event.previous_digest,
            })
            if not hmac.compare_digest(expected, event.event_digest):
                return False
            previous = event.event_digest
        return True

    @property
    def tail_digest(self) -> str:
        return self._events[-1].event_digest if self._events else self.GENESIS

    def snapshot(self) -> tuple[JournalEventV106, ...]:
        return tuple(self._events)


@dataclass(frozen=True, slots=True)
class RolloutActionV106:
    action_id: str
    qualification_id: str
    action: RolloutActionTypeV106
    created_at: datetime
    approver_a: str
    approver_b: str
    evidence_digest: str
    state_digest: str
    idempotency_key: str
    key_id: str
    signature: str
    status: RolloutActionStatusV106 = RolloutActionStatusV106.PENDING
    attempt_count: int = 0
    receipt_digest: str | None = None

    def __post_init__(self) -> None:
        for name in ("action_id", "qualification_id", "approver_a", "approver_b", "idempotency_key", "key_id"):
            _validate_id(getattr(self, name), name)
        _require_distinct_approvers(self.approver_a, self.approver_b)
        _ensure_utc(self.created_at)
        _validate_hex(self.evidence_digest, "evidence_digest")
        _validate_hex(self.state_digest, "state_digest")
        _validate_hex(self.signature, "signature")
        if self.attempt_count not in {0, 1}:
            raise ValidationErrorV106("rollout action supports at most one mutation attempt")
        if self.receipt_digest is not None:
            _validate_hex(self.receipt_digest, "receipt_digest")

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("signature")
        payload.pop("status")
        payload.pop("attempt_count")
        payload.pop("receipt_digest")
        return payload

    @classmethod
    def sign(
        cls,
        *,
        action_id: str,
        qualification_id: str,
        action: RolloutActionTypeV106,
        created_at: datetime,
        approver_a: str,
        approver_b: str,
        evidence_digest: str,
        state_digest: str,
        idempotency_key: str,
        key_id: str,
        secret: bytes,
    ) -> "RolloutActionV106":
        if len(secret) < 32:
            raise ValidationErrorV106("action signing secret must be at least 32 bytes")
        unsigned = cls(action_id, qualification_id, action, created_at, approver_a, approver_b, evidence_digest, state_digest, idempotency_key, key_id, "0" * 64)
        return replace(unsigned, signature=hmac.new(secret, _canonical(unsigned.to_payload()), hashlib.sha256).hexdigest())

    def verify(self, keyring: Mapping[str, bytes]) -> None:
        secret = keyring.get(self.key_id)
        if secret is None or len(secret) < 32:
            raise SignatureErrorV106("unknown action signing key")
        expected = hmac.new(secret, _canonical(self.to_payload()), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, self.signature):
            raise SignatureErrorV106("rollout action signature mismatch")

    def claim(self) -> "RolloutActionV106":
        if self.status != RolloutActionStatusV106.PENDING or self.attempt_count != 0:
            raise StateTransitionErrorV106("rollout action cannot be claimed")
        return replace(self, status=RolloutActionStatusV106.CLAIMED, attempt_count=1)

    def acknowledge(self, *, success: bool, receipt_digest: str) -> "RolloutActionV106":
        _validate_hex(receipt_digest, "receipt_digest")
        if self.status != RolloutActionStatusV106.CLAIMED or self.attempt_count != 1:
            raise StateTransitionErrorV106("rollout action was not claimed")
        return replace(self, status=RolloutActionStatusV106.ACKED if success else RolloutActionStatusV106.FAILED, receipt_digest=receipt_digest)


@dataclass(slots=True)
class DeploymentQualificationCoordinatorV106:
    qualification_id: str
    policy: DeploymentPolicyV106
    manifest: SignedDeploymentManifestV106
    keyring: Mapping[str, bytes]
    state: QualificationStateV106 = QualificationStateV106.PLANNED
    failure_samples: int = 0
    preflight: GateSetV106 | None = None
    observations: list[ObservationSampleV106] = field(default_factory=list)
    observation_gates: list[GateSetV106] = field(default_factory=list)
    action: RolloutActionV106 | None = None
    journal: QualificationJournalV106 = field(default_factory=QualificationJournalV106)

    def __post_init__(self) -> None:
        _validate_id(self.qualification_id, "qualification_id")

    @property
    def state_digest(self) -> str:
        return _digest({
            "qualification_id": self.qualification_id,
            "state": self.state,
            "manifest": self.manifest.manifest_digest,
            "preflight": self.preflight.digest if self.preflight else None,
            "observations": [sample.digest for sample in self.observations],
            "failure_samples": self.failure_samples,
            "action": self.action.to_payload() if self.action else None,
            "journal_tail": self.journal.tail_digest,
        })

    def start(
        self,
        *,
        snapshot: KubernetesDeploymentSnapshotV106,
        dependencies: DependencySnapshotV106,
        now: datetime,
        replay_ledger: ManifestReplayLedgerV106,
    ) -> GateSetV106:
        if self.state != QualificationStateV106.PLANNED:
            raise StateTransitionErrorV106("qualification already started")
        current = _ensure_utc(now)
        self.state = QualificationStateV106.PREFLIGHT
        self.manifest.verify(policy=self.policy, keyring=self.keyring, now=current, replay_ledger=replay_ledger)
        result = evaluate_preflight_v106(policy=self.policy, manifest=self.manifest, snapshot=snapshot, dependencies=dependencies, now=current)
        self.preflight = result
        self.journal.append("PREFLIGHT_EVALUATED", {"gates": result.digest, "snapshot": snapshot.digest, "dependencies": dependencies.digest}, current)
        if result.passed:
            self.state = QualificationStateV106.CANARY
            self.journal.append("CANARY_STARTED", {"manifest": self.manifest.manifest_digest}, current)
        else:
            self.state = QualificationStateV106.BLOCKED
            self.journal.append("QUALIFICATION_BLOCKED", {"failures": [gate.digest for gate in result.critical_failures]}, current)
        return result

    def record_observation(self, sample: ObservationSampleV106) -> GateSetV106:
        if self.state not in {QualificationStateV106.CANARY, QualificationStateV106.OBSERVING}:
            raise StateTransitionErrorV106("qualification is not accepting observations")
        if any(existing.sample_id == sample.sample_id for existing in self.observations):
            raise ReplayErrorV106("observation sample already recorded")
        if self.observations and _ensure_utc(sample.observed_at) <= _ensure_utc(self.observations[-1].observed_at):
            raise ValidationErrorV106("observation time must increase monotonically")
        gates = evaluate_observation_v106(self.policy, sample)
        self.observations.append(sample)
        self.observation_gates.append(gates)
        self.state = QualificationStateV106.OBSERVING
        self.journal.append("OBSERVATION_RECORDED", {"sample": sample.digest, "gates": gates.digest}, sample.observed_at)
        if not gates.passed:
            self.failure_samples += 1
            critical_boundary = sample.broker_mutation_count > 0 or sample.external_order_routing_allowed or sample.live_trading_allowed
            if critical_boundary:
                self.state = QualificationStateV106.QUARANTINED
                self.journal.append("QUALIFICATION_QUARANTINED", {"sample": sample.digest}, sample.observed_at)
            elif self.failure_samples > self.policy.max_failure_samples:
                self.state = QualificationStateV106.BLOCKED
                self.journal.append("QUALIFICATION_BLOCKED", {"sample": sample.digest, "failure_samples": self.failure_samples}, sample.observed_at)
        return gates

    def assess_promotable(self, now: datetime) -> bool:
        if self.state != QualificationStateV106.OBSERVING:
            return False
        current = _ensure_utc(now)
        if len(self.observations) < self.policy.min_observation_samples:
            return False
        elapsed = (_ensure_utc(self.observations[-1].observed_at) - _ensure_utc(self.observations[0].observed_at)).total_seconds()
        if elapsed < self.policy.min_observation_seconds:
            return False
        if self.failure_samples > self.policy.max_failure_samples:
            return False
        if (current - _ensure_utc(self.observations[-1].observed_at)).total_seconds() > self.policy.heartbeat_max_age_seconds:
            return False
        if any(not gates.passed for gates in self.observation_gates):
            return False
        self.state = QualificationStateV106.PROMOTABLE
        self.journal.append("PROMOTION_GATES_PASSED", {"samples": [sample.digest for sample in self.observations]}, current)
        return True

    def create_action(
        self,
        *,
        action_id: str,
        action: RolloutActionTypeV106,
        approver_a: str,
        approver_b: str,
        key_id: str,
        secret: bytes,
        now: datetime,
        reason_digest: str,
    ) -> RolloutActionV106:
        current = _ensure_utc(now)
        _validate_hex(reason_digest, "reason_digest")
        if self.action is not None:
            raise StateTransitionErrorV106("qualification already has a rollout action")
        if action == RolloutActionTypeV106.PROMOTE and self.state != QualificationStateV106.PROMOTABLE:
            raise StateTransitionErrorV106("qualification is not promotable")
        if action == RolloutActionTypeV106.ROLLBACK and self.state not in {
            QualificationStateV106.CANARY,
            QualificationStateV106.OBSERVING,
            QualificationStateV106.PROMOTABLE,
            QualificationStateV106.BLOCKED,
            QualificationStateV106.QUARANTINED,
        }:
            raise StateTransitionErrorV106("rollback is not valid in current state")
        action_record = RolloutActionV106.sign(
            action_id=action_id,
            qualification_id=self.qualification_id,
            action=action,
            created_at=current,
            approver_a=approver_a,
            approver_b=approver_b,
            evidence_digest=reason_digest,
            state_digest=self.state_digest,
            idempotency_key=f"{self.qualification_id}:{action.value}:{self.manifest.generation}",
            key_id=key_id,
            secret=secret,
        )
        action_record.verify(self.keyring | {key_id: secret})
        self.action = action_record
        self.state = QualificationStateV106.PROMOTION_PENDING if action == RolloutActionTypeV106.PROMOTE else QualificationStateV106.ROLLBACK_PENDING
        self.journal.append("ROLLOUT_ACTION_CREATED", action_record.to_payload(), current)
        return action_record

    def claim_action(self) -> RolloutActionV106:
        if self.action is None:
            raise StateTransitionErrorV106("no rollout action")
        self.action = self.action.claim()
        self.journal.append("ROLLOUT_ACTION_CLAIMED", self.action.to_payload(), self.action.created_at)
        return self.action

    def acknowledge_action(self, *, success: bool, receipt_digest: str, observed_at: datetime) -> RolloutActionV106:
        current = _ensure_utc(observed_at)
        if self.action is None:
            raise StateTransitionErrorV106("no rollout action")
        self.action = self.action.acknowledge(success=success, receipt_digest=receipt_digest)
        if success and self.action.action == RolloutActionTypeV106.PROMOTE:
            self.state = QualificationStateV106.PROMOTED
        elif success and self.action.action == RolloutActionTypeV106.ROLLBACK:
            self.state = QualificationStateV106.ROLLED_BACK
        else:
            self.state = QualificationStateV106.QUARANTINED
        self.journal.append("ROLLOUT_ACTION_ACKNOWLEDGED", {"action": self.action.to_payload(), "success": success, "receipt": receipt_digest}, current)
        return self.action

    def complete_rollout(
        self,
        *,
        snapshot: KubernetesDeploymentSnapshotV106,
        dependencies: DependencySnapshotV106,
        now: datetime,
    ) -> GateSetV106:
        if self.state != QualificationStateV106.PROMOTED:
            raise StateTransitionErrorV106("rollout has not been promoted")
        current = _ensure_utc(now)
        gates = evaluate_preflight_v106(policy=self.policy, manifest=self.manifest, snapshot=snapshot, dependencies=dependencies, now=current)
        full_ready = snapshot.desired_replicas == self.manifest.replicas and snapshot.available_replicas >= min(self.manifest.replicas, self.policy.min_ready_replicas)
        full_gate = GateEvaluationV106("full_rollout_ready", full_ready, GateSeverityV106.CRITICAL, "target replica count is deployed and ready", snapshot.digest)
        combined = GateSetV106(gates.gates + (full_gate,))
        if combined.passed:
            self.state = QualificationStateV106.COMPLETED
            self.journal.append("QUALIFICATION_COMPLETED", {"gates": combined.digest, "snapshot": snapshot.digest}, current)
        else:
            self.state = QualificationStateV106.QUARANTINED
            self.journal.append("QUALIFICATION_QUARANTINED", {"gates": combined.digest, "snapshot": snapshot.digest}, current)
        return combined


@dataclass(frozen=True, slots=True)
class CertificateSnapshotV106:
    worker_id: str
    identity_generation: int
    fingerprint: str
    serial_digest: str
    issuer_digest: str
    not_before: datetime
    not_after: datetime
    heartbeat_at: datetime
    active_claims: int

    def __post_init__(self) -> None:
        _validate_id(self.worker_id, "worker_id")
        if self.identity_generation <= 0 or self.active_claims < 0:
            raise ValidationErrorV106("invalid certificate generation or active claims")
        for name in ("fingerprint", "serial_digest", "issuer_digest"):
            _validate_hex(getattr(self, name), name)
        if not (_ensure_utc(self.not_before) < _ensure_utc(self.not_after)):
            raise ValidationErrorV106("invalid certificate interval")
        _ensure_utc(self.heartbeat_at)

    @property
    def digest(self) -> str:
        return _digest(asdict(self))


@dataclass(slots=True)
class CertificateRenewalDrillV106:
    drill_id: str
    policy: DeploymentPolicyV106
    old_certificate: CertificateSnapshotV106
    approver_a: str
    approver_b: str
    state: CertificateDrillStateV106 = CertificateDrillStateV106.PLANNED
    new_certificate: CertificateSnapshotV106 | None = None
    old_revoked_at: datetime | None = None
    journal: QualificationJournalV106 = field(default_factory=QualificationJournalV106)

    def __post_init__(self) -> None:
        _validate_id(self.drill_id, "drill_id")
        _require_distinct_approvers(self.approver_a, self.approver_b)

    def issue(self, certificate: CertificateSnapshotV106, observed_at: datetime) -> None:
        current = _ensure_utc(observed_at)
        if self.state != CertificateDrillStateV106.PLANNED:
            raise StateTransitionErrorV106("certificate drill is not planned")
        if certificate.worker_id != self.old_certificate.worker_id:
            raise ValidationErrorV106("certificate worker mismatch")
        if certificate.identity_generation != self.old_certificate.identity_generation + 1:
            raise ValidationErrorV106("certificate identity generation must increment by one")
        if certificate.fingerprint == self.old_certificate.fingerprint:
            raise ValidationErrorV106("new certificate fingerprint must differ")
        if _ensure_utc(certificate.not_before) > current or _ensure_utc(certificate.not_after) <= current:
            raise ValidationErrorV106("new certificate is not currently valid")
        self.new_certificate = certificate
        self.state = CertificateDrillStateV106.ISSUED
        self.journal.append("CERTIFICATE_ISSUED", certificate.digest, current)

    def activate(self, observed_at: datetime) -> None:
        current = _ensure_utc(observed_at)
        if self.state != CertificateDrillStateV106.ISSUED or self.new_certificate is None:
            raise StateTransitionErrorV106("new certificate has not been issued")
        if self.old_certificate.active_claims != 0 or self.new_certificate.active_claims != 0:
            raise ValidationErrorV106("certificate activation requires zero active claims")
        overlap = (_ensure_utc(self.old_certificate.not_after) - _ensure_utc(self.new_certificate.not_before)).total_seconds()
        if overlap < 0 or overlap > self.policy.cert_max_overlap_seconds:
            raise ValidationErrorV106("certificate overlap outside policy")
        self.state = CertificateDrillStateV106.ACTIVATED
        self.journal.append("CERTIFICATE_ACTIVATED", self.new_certificate.digest, current)

    def revoke_old(self, observed_at: datetime) -> None:
        current = _ensure_utc(observed_at)
        if self.state != CertificateDrillStateV106.ACTIVATED:
            raise StateTransitionErrorV106("new certificate is not active")
        self.old_revoked_at = current
        self.state = CertificateDrillStateV106.OLD_REVOKED
        self.journal.append("OLD_CERTIFICATE_REVOKED", self.old_certificate.digest, current)

    def verify(self, observed: CertificateSnapshotV106, observed_at: datetime) -> bool:
        current = _ensure_utc(observed_at)
        if self.state != CertificateDrillStateV106.OLD_REVOKED or self.new_certificate is None or self.old_revoked_at is None:
            raise StateTransitionErrorV106("old certificate has not been revoked")
        valid = (
            observed.worker_id == self.new_certificate.worker_id
            and observed.identity_generation == self.new_certificate.identity_generation
            and observed.fingerprint == self.new_certificate.fingerprint
            and observed.fingerprint != self.old_certificate.fingerprint
            and (current - _ensure_utc(observed.heartbeat_at)).total_seconds() <= self.policy.heartbeat_max_age_seconds
            and (_ensure_utc(observed.not_after) - current).total_seconds() >= self.policy.cert_min_remaining_seconds
            and observed.active_claims == 0
        )
        self.state = CertificateDrillStateV106.VERIFIED if valid else CertificateDrillStateV106.FAILED
        self.journal.append("CERTIFICATE_DRILL_VERIFIED" if valid else "CERTIFICATE_DRILL_FAILED", observed.digest, current)
        return valid


@dataclass(frozen=True, slots=True)
class BackupManifestV106:
    backup_id: str
    source_environment: str
    created_at: datetime
    completed_at: datetime
    object_digest: str
    size_bytes: int
    postgres_lsn: str
    schema_version: str
    encrypted: bool
    kms_key_id: str
    integrity_digest: str

    def __post_init__(self) -> None:
        for name in ("backup_id", "source_environment", "postgres_lsn", "schema_version", "kms_key_id"):
            _validate_id(getattr(self, name), name)
        if not (_ensure_utc(self.created_at) <= _ensure_utc(self.completed_at)):
            raise ValidationErrorV106("backup completion precedes creation")
        _validate_hex(self.object_digest, "object_digest")
        _validate_hex(self.integrity_digest, "integrity_digest")
        if self.size_bytes <= 0:
            raise ValidationErrorV106("backup size must be positive")
        if not self.encrypted:
            raise ValidationErrorV106("backup must be encrypted")

    @property
    def digest(self) -> str:
        return _digest(asdict(self))


@dataclass(frozen=True, slots=True)
class RestoreEvidenceV106:
    target_environment: str
    started_at: datetime
    completed_at: datetime
    restored_lsn: str
    schema_version: str
    integrity_digest: str
    postgres_ready: bool
    object_storage_ready: bool
    external_order_routing_allowed: bool
    live_trading_allowed: bool

    def __post_init__(self) -> None:
        _validate_id(self.target_environment, "target_environment")
        _validate_id(self.restored_lsn, "restored_lsn")
        _validate_id(self.schema_version, "schema_version")
        if not (_ensure_utc(self.started_at) < _ensure_utc(self.completed_at)):
            raise ValidationErrorV106("restore completion must follow start")
        _validate_hex(self.integrity_digest, "integrity_digest")

    @property
    def digest(self) -> str:
        return _digest(asdict(self))


@dataclass(slots=True)
class DisasterRecoveryDrillV106:
    drill_id: str
    policy: DeploymentPolicyV106
    backup: BackupManifestV106
    state: DisasterRecoveryStateV106 = DisasterRecoveryStateV106.PLANNED
    journal: QualificationJournalV106 = field(default_factory=QualificationJournalV106)

    def __post_init__(self) -> None:
        _validate_id(self.drill_id, "drill_id")

    def start(self, observed_at: datetime) -> None:
        current = _ensure_utc(observed_at)
        if self.state != DisasterRecoveryStateV106.PLANNED:
            raise StateTransitionErrorV106("disaster recovery drill already started")
        if self.backup.source_environment != self.policy.environment:
            raise ValidationErrorV106("backup source environment mismatch")
        if (current - _ensure_utc(self.backup.completed_at)).total_seconds() > self.policy.backup_max_age_seconds:
            raise ValidationErrorV106("backup is too old for recovery drill")
        self.state = DisasterRecoveryStateV106.RESTORING
        self.journal.append("DR_RESTORE_STARTED", self.backup.digest, current)

    def verify(self, restore: RestoreEvidenceV106, observed_at: datetime) -> GateSetV106:
        current = _ensure_utc(observed_at)
        if self.state != DisasterRecoveryStateV106.RESTORING:
            raise StateTransitionErrorV106("disaster recovery drill is not restoring")
        self.state = DisasterRecoveryStateV106.VERIFYING
        evidence = _digest({"backup": self.backup.digest, "restore": restore.digest})
        rpo = (_ensure_utc(restore.started_at) - _ensure_utc(self.backup.completed_at)).total_seconds()
        rto = (_ensure_utc(restore.completed_at) - _ensure_utc(restore.started_at)).total_seconds()
        gates = GateSetV106((
            GateEvaluationV106("isolated_target", restore.target_environment.startswith("drill-") and restore.target_environment != self.policy.environment, GateSeverityV106.CRITICAL, "restore target is isolated from production", evidence),
            GateEvaluationV106("rpo", 0 <= rpo <= self.policy.max_rpo_seconds, GateSeverityV106.CRITICAL, "recovery point objective is satisfied", evidence),
            GateEvaluationV106("rto", 0 < rto <= self.policy.max_rto_seconds, GateSeverityV106.CRITICAL, "recovery time objective is satisfied", evidence),
            GateEvaluationV106("postgres_lsn", restore.restored_lsn == self.backup.postgres_lsn, GateSeverityV106.CRITICAL, "restored LSN matches backup", evidence),
            GateEvaluationV106("schema_version", restore.schema_version == self.backup.schema_version, GateSeverityV106.CRITICAL, "restored schema version matches backup", evidence),
            GateEvaluationV106("integrity", restore.integrity_digest == self.backup.integrity_digest, GateSeverityV106.CRITICAL, "restored integrity digest matches backup", evidence),
            GateEvaluationV106("dependencies", restore.postgres_ready and restore.object_storage_ready, GateSeverityV106.CRITICAL, "restored dependencies are ready", evidence),
            GateEvaluationV106("routing_boundary", not restore.external_order_routing_allowed and not restore.live_trading_allowed, GateSeverityV106.CRITICAL, "restored drill environment cannot route orders or trade live", evidence),
            GateEvaluationV106("completion_freshness", current >= _ensure_utc(restore.completed_at), GateSeverityV106.CRITICAL, "restore completion is not in the future", evidence),
        ))
        self.state = DisasterRecoveryStateV106.PASSED if gates.passed else DisasterRecoveryStateV106.QUARANTINED
        self.journal.append("DR_DRILL_PASSED" if gates.passed else "DR_DRILL_QUARANTINED", gates.digest, current)
        return gates


@dataclass(frozen=True, slots=True)
class QualificationEvidenceBundleV106:
    qualification_id: str
    policy_digest: str
    manifest_digest: str
    preflight_digest: str
    observation_digests: tuple[str, ...]
    action_digest: str | None
    journal_tail_digest: str
    final_state: QualificationStateV106

    def __post_init__(self) -> None:
        _validate_id(self.qualification_id, "qualification_id")
        for name in ("policy_digest", "manifest_digest", "preflight_digest", "journal_tail_digest"):
            _validate_hex(getattr(self, name), name)
        for digest in self.observation_digests:
            _validate_hex(digest, "observation_digest")
        if self.action_digest is not None:
            _validate_hex(self.action_digest, "action_digest")

    @property
    def bundle_digest(self) -> str:
        return _digest(asdict(self))

    @classmethod
    def from_coordinator(cls, coordinator: DeploymentQualificationCoordinatorV106) -> "QualificationEvidenceBundleV106":
        if coordinator.preflight is None:
            raise ValidationErrorV106("qualification has no preflight evidence")
        action_digest = _digest(coordinator.action.to_payload()) if coordinator.action else None
        return cls(
            qualification_id=coordinator.qualification_id,
            policy_digest=coordinator.policy.policy_digest,
            manifest_digest=coordinator.manifest.manifest_digest,
            preflight_digest=coordinator.preflight.digest,
            observation_digests=tuple(sample.digest for sample in coordinator.observations),
            action_digest=action_digest,
            journal_tail_digest=coordinator.journal.tail_digest,
            final_state=coordinator.state,
        )


EXTERNAL_ORDER_ROUTING_ALLOWED_V106 = False
LIVE_TRADING_ALLOWED_V106 = False
KUBERNETES_MUTATIONS_ALLOWED_V106 = False
