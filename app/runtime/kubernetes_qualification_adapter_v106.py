from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import quote, urlparse

from app.runtime.deployment_qualification_v106 import (
    DisruptionBudgetSnapshotV106,
    KubernetesDeploymentSnapshotV106,
    NetworkPolicySnapshotV106,
    PodSnapshotV106,
    ValidationErrorV106,
)

UTC = timezone.utc


class KubernetesAdapterErrorV106(RuntimeError):
    pass


class KubernetesTransportErrorV106(KubernetesAdapterErrorV106):
    pass


class KubernetesProtocolErrorV106(KubernetesAdapterErrorV106):
    pass


@dataclass(frozen=True, slots=True)
class KubernetesResponseV106:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


class KubernetesReadOnlyTransportV106(Protocol):
    def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        timeout_seconds: float,
        tls_verify: bool,
        allow_redirects: bool,
        max_response_bytes: int,
    ) -> KubernetesResponseV106: ...


@dataclass(frozen=True, slots=True)
class KubernetesEndpointV106:
    api_base: str
    allowed_hosts: tuple[str, ...]
    namespace: str
    bearer_token: str
    timeout_seconds: float = 5.0
    max_response_bytes: int = 2_000_000

    def __post_init__(self) -> None:
        parsed = urlparse(self.api_base)
        if parsed.scheme != "https" or not parsed.hostname or parsed.path not in {"", "/"}:
            raise ValidationErrorV106("Kubernetes API base must be an HTTPS origin")
        if parsed.hostname not in self.allowed_hosts:
            raise ValidationErrorV106("Kubernetes API host is not allowlisted")
        if not self.namespace or "/" in self.namespace or self.namespace in {".", ".."}:
            raise ValidationErrorV106("invalid Kubernetes namespace")
        if not self.bearer_token or len(self.bearer_token) < 16:
            raise ValidationErrorV106("Kubernetes bearer token is missing or weak")
        if self.timeout_seconds <= 0 or self.timeout_seconds > 30:
            raise ValidationErrorV106("invalid Kubernetes timeout")
        if self.max_response_bytes <= 0 or self.max_response_bytes > 10_000_000:
            raise ValidationErrorV106("invalid Kubernetes response limit")

    @property
    def host(self) -> str:
        hostname = urlparse(self.api_base).hostname
        assert hostname is not None
        return hostname


class KubernetesReadOnlyAdapterV106:
    def __init__(self, endpoint: KubernetesEndpointV106, transport: KubernetesReadOnlyTransportV106) -> None:
        self._endpoint = endpoint
        self._transport = transport

    def _get_json(self, path: str) -> dict[str, Any]:
        if not path.startswith("/") or ".." in path or "//" in path:
            raise KubernetesProtocolErrorV106("invalid Kubernetes API path")
        allowed_prefixes = (
            f"/apis/apps/v1/namespaces/{quote(self._endpoint.namespace, safe='')}/deployments/",
            f"/api/v1/namespaces/{quote(self._endpoint.namespace, safe='')}/pods",
            f"/apis/policy/v1/namespaces/{quote(self._endpoint.namespace, safe='')}/poddisruptionbudgets/",
            f"/apis/networking.k8s.io/v1/namespaces/{quote(self._endpoint.namespace, safe='')}/networkpolicies",
        )
        if not any(path.startswith(prefix) for prefix in allowed_prefixes):
            raise KubernetesProtocolErrorV106("Kubernetes API path is outside the read-only qualification allowlist")
        url = self._endpoint.api_base.rstrip("/") + path
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != self._endpoint.host or parsed.hostname not in self._endpoint.allowed_hosts:
            raise KubernetesProtocolErrorV106("Kubernetes request escaped the configured origin")
        response = self._transport.request(
            method="GET",
            url=url,
            headers={"Accept": "application/json", "Authorization": f"Bearer {self._endpoint.bearer_token}"},
            timeout_seconds=self._endpoint.timeout_seconds,
            tls_verify=True,
            allow_redirects=False,
            max_response_bytes=self._endpoint.max_response_bytes,
        )
        if response.status_code in {301, 302, 303, 307, 308}:
            raise KubernetesTransportErrorV106("Kubernetes redirects are forbidden")
        if response.status_code != 200:
            raise KubernetesTransportErrorV106(f"Kubernetes API returned {response.status_code}")
        if len(response.body) > self._endpoint.max_response_bytes:
            raise KubernetesTransportErrorV106("Kubernetes response exceeded the configured limit")
        content_type = response.headers.get("content-type", response.headers.get("Content-Type", ""))
        if "application/json" not in content_type.lower():
            raise KubernetesProtocolErrorV106("Kubernetes API returned a non-JSON response")
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise KubernetesProtocolErrorV106("Kubernetes API returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise KubernetesProtocolErrorV106("Kubernetes API payload must be an object")
        return payload

    @staticmethod
    def _parse_time(value: str, name: str) -> datetime:
        if not isinstance(value, str) or not value:
            raise KubernetesProtocolErrorV106(f"missing {name}")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise KubernetesProtocolErrorV106(f"invalid {name}") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise KubernetesProtocolErrorV106(f"{name} must include timezone")
        return parsed.astimezone(UTC)

    @staticmethod
    def _int_annotation(annotations: Mapping[str, Any], key: str, default: int = 0) -> int:
        value = annotations.get(key, str(default))
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise KubernetesProtocolErrorV106(f"annotation {key} must be an integer") from exc
        if number < 0:
            raise KubernetesProtocolErrorV106(f"annotation {key} must be non-negative")
        return number

    @staticmethod
    def _bool_annotation(annotations: Mapping[str, Any], key: str, default: bool = False) -> bool:
        raw = annotations.get(key, "true" if default else "false")
        if raw not in {"true", "false", True, False}:
            raise KubernetesProtocolErrorV106(f"annotation {key} must be boolean")
        return raw in {"true", True}

    @staticmethod
    def _container_digest(pod: Mapping[str, Any]) -> str:
        containers = pod.get("spec", {}).get("containers", [])
        if not isinstance(containers, list) or len(containers) != 1:
            raise KubernetesProtocolErrorV106("qualification pod must have exactly one container")
        image = containers[0].get("image", "")
        if "@sha256:" not in image:
            raise KubernetesProtocolErrorV106("qualification image must be digest-pinned")
        return "sha256:" + image.rsplit("@sha256:", 1)[1]

    @staticmethod
    def _ready(pod: Mapping[str, Any]) -> bool:
        conditions = pod.get("status", {}).get("conditions", [])
        return any(condition.get("type") == "Ready" and condition.get("status") == "True" for condition in conditions if isinstance(condition, dict))

    @staticmethod
    def _restart_count(pod: Mapping[str, Any]) -> int:
        statuses = pod.get("status", {}).get("containerStatuses", [])
        if not isinstance(statuses, list):
            raise KubernetesProtocolErrorV106("invalid container statuses")
        return sum(int(status.get("restartCount", 0)) for status in statuses if isinstance(status, dict))

    @staticmethod
    def _normalize_network_policy(payload: Mapping[str, Any]) -> NetworkPolicySnapshotV106:
        items = payload.get("items", [])
        if not isinstance(items, list):
            raise KubernetesProtocolErrorV106("network policy items must be a list")
        default_deny_ingress = False
        default_deny_egress = False
        allowed: set[str] = set()
        broad_cidrs: set[str] = set()
        live_hosts: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            metadata = item.get("metadata", {})
            raw_annotations = metadata.get("annotations")
            if raw_annotations is None:
                annotations: Mapping[str, Any] = {}
            elif not isinstance(raw_annotations, dict):
                raise KubernetesProtocolErrorV106("network policy annotations must be an object")
            else:
                annotations = raw_annotations
            spec = item.get("spec", {})
            policy_types = set(spec.get("policyTypes", []) or [])
            if "Ingress" in policy_types and spec.get("ingress", []) == []:
                default_deny_ingress = True
            if "Egress" in policy_types and spec.get("egress", []) == []:
                default_deny_egress = True
            raw_allowed = annotations.get("astra.openai/allowed-egress", "")
            if raw_allowed:
                allowed.update(part.strip() for part in str(raw_allowed).split(",") if part.strip())
            raw_live = annotations.get("astra.openai/live-hosts", "")
            if raw_live:
                live_hosts.update(part.strip() for part in str(raw_live).split(",") if part.strip())
            for egress in spec.get("egress", []) or []:
                if not isinstance(egress, dict):
                    continue
                for peer in egress.get("to", []) or []:
                    if not isinstance(peer, dict):
                        continue
                    cidr = (peer.get("ipBlock") or {}).get("cidr")
                    if cidr in {"0.0.0.0/0", "::/0"}:
                        broad_cidrs.add(cidr)
        return NetworkPolicySnapshotV106(
            default_deny_ingress=default_deny_ingress,
            default_deny_egress=default_deny_egress,
            allowed_egress=tuple(sorted(allowed)),
            broad_cidrs=tuple(sorted(broad_cidrs)),
            live_hosts=tuple(sorted(live_hosts)),
        )

    @staticmethod
    def _normalize_pdb(payload: Mapping[str, Any]) -> DisruptionBudgetSnapshotV106:
        spec = payload.get("spec", {})
        min_available = spec.get("minAvailable")
        max_unavailable = spec.get("maxUnavailable")
        if isinstance(min_available, str) or isinstance(max_unavailable, str):
            raise KubernetesProtocolErrorV106("percentage PDB values are not accepted for deterministic qualification")
        return DisruptionBudgetSnapshotV106(
            min_available=int(min_available) if min_available is not None else None,
            max_unavailable=int(max_unavailable) if max_unavailable is not None else None,
            unhealthy_pod_eviction_policy=str(spec.get("unhealthyPodEvictionPolicy", "IfHealthyBudget")),
        )

    def collect_snapshot(
        self,
        *,
        deployment_name: str,
        pdb_name: str,
        cluster: str,
        observed_at: datetime,
    ) -> KubernetesDeploymentSnapshotV106:
        if not deployment_name or "/" in deployment_name or not pdb_name or "/" in pdb_name:
            raise ValidationErrorV106("invalid deployment or PDB name")
        namespace = quote(self._endpoint.namespace, safe="")
        deployment = self._get_json(f"/apis/apps/v1/namespaces/{namespace}/deployments/{quote(deployment_name, safe='')}")
        selector = deployment.get("spec", {}).get("selector", {}).get("matchLabels", {})
        if not isinstance(selector, dict) or not selector:
            raise KubernetesProtocolErrorV106("deployment selector is missing")
        label_selector = ",".join(f"{key}={value}" for key, value in sorted(selector.items()))
        pods_payload = self._get_json(f"/api/v1/namespaces/{namespace}/pods?labelSelector={quote(label_selector, safe='=,')}")
        pdb_payload = self._get_json(f"/apis/policy/v1/namespaces/{namespace}/poddisruptionbudgets/{quote(pdb_name, safe='')}")
        network_payload = self._get_json(f"/apis/networking.k8s.io/v1/namespaces/{namespace}/networkpolicies")

        metadata = deployment.get("metadata", {})
        annotations = metadata.get("annotations", {}) or {}
        template_spec = deployment.get("spec", {}).get("template", {}).get("spec", {})
        pods: list[PodSnapshotV106] = []
        zone_counts: dict[str, int] = {}
        for pod in pods_payload.get("items", []) or []:
            if not isinstance(pod, dict):
                raise KubernetesProtocolErrorV106("pod item must be an object")
            pod_metadata = pod.get("metadata", {})
            pod_annotations = pod_metadata.get("annotations", {}) or {}
            labels = pod_metadata.get("labels", {}) or {}
            zone = str(labels.get("topology.kubernetes.io/zone", ""))
            zone_counts[zone] = zone_counts.get(zone, 0) + 1
            pods.append(PodSnapshotV106(
                pod_uid=str(pod_metadata.get("uid", "")),
                worker_id=str(labels.get("astra.openai/worker-id", "")),
                zone=zone,
                image_digest=self._container_digest(pod),
                config_digest=str(pod_annotations.get("astra.openai/config-digest", "")),
                ready=self._ready(pod),
                is_canary=str(labels.get("astra.openai/canary", "false")).lower() == "true",
                restart_count=self._restart_count(pod),
                heartbeat_at=self._parse_time(str(pod_annotations.get("astra.openai/heartbeat-at", "")), "heartbeat-at"),
                certificate_not_after=self._parse_time(str(pod_annotations.get("astra.openai/certificate-not-after", "")), "certificate-not-after"),
                active_claims=self._int_annotation(pod_annotations, "astra.openai/active-claims"),
                evidence_pending=self._int_annotation(pod_annotations, "astra.openai/evidence-pending"),
                broker_mutation_count=self._int_annotation(pod_annotations, "astra.openai/broker-mutation-count"),
            ))
        ready = sum(1 for pod in pods if pod.ready)
        canary_ready = sum(1 for pod in pods if pod.ready and pod.is_canary)
        return KubernetesDeploymentSnapshotV106(
            cluster=cluster,
            namespace=self._endpoint.namespace,
            service_account=str(template_spec.get("serviceAccountName", "")),
            deployment_id=str(metadata.get("uid", "")),
            generation=int(metadata.get("generation", 0)),
            observed_at=observed_at,
            desired_replicas=int(deployment.get("spec", {}).get("replicas", 0)),
            available_replicas=ready,
            canary_ready_replicas=canary_ready,
            zone_replicas=tuple(sorted(zone_counts.items())),
            pods=tuple(pods),
            network_policy=self._normalize_network_policy(network_payload),
            disruption_budget=self._normalize_pdb(pdb_payload),
            external_order_routing_allowed=self._bool_annotation(annotations, "astra.openai/external-order-routing"),
            live_trading_allowed=self._bool_annotation(annotations, "astra.openai/live-trading"),
        )


KUBERNETES_MUTATIONS_ALLOWED_V106 = False
