from __future__ import annotations

from dataclasses import dataclass, field
import json
import threading
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlparse

from app.runtime.rollout_execution_v107 import (
    DeploymentRuntimeSnapshotV107,
    SignedDeploymentExecutionCommandV107,
    ValidationErrorV107,
    digest_v107,
)


class KubernetesAdapterErrorV107(RuntimeError):
    pass


class KubernetesTransportErrorV107(KubernetesAdapterErrorV107):
    pass


class KubernetesResponseErrorV107(KubernetesAdapterErrorV107):
    pass


class KubernetesMutationRejectedV107(KubernetesAdapterErrorV107):
    pass


class KubernetesPreconditionFailedV107(KubernetesMutationRejectedV107):
    pass


class KubernetesAmbiguousMutationV107(KubernetesAdapterErrorV107):
    pass


class KubernetesMutationReplayV107(KubernetesAdapterErrorV107):
    pass


@dataclass(frozen=True, slots=True)
class KubernetesHttpRequestV107:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None
    timeout_seconds: float
    tls_verify: bool
    allow_redirects: bool


@dataclass(frozen=True, slots=True)
class KubernetesHttpResponseV107:
    status_code: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)


TransportV107 = Callable[[KubernetesHttpRequestV107], KubernetesHttpResponseV107]


@dataclass(frozen=True, slots=True)
class KubernetesDeploymentObservationV107:
    snapshot: DeploymentRuntimeSnapshotV107
    annotations: Mapping[str, str]


@dataclass(slots=True)
class KubernetesRolloutAdapterV107:
    api_base: str
    cluster: str
    bearer_token: str = field(repr=False)
    transport: TransportV107 = field(repr=False)
    allowed_hosts: tuple[str, ...] = ()
    timeout_seconds: float = 10.0
    max_response_bytes: int = 2_000_000
    _attempted_commands: set[str] = field(default_factory=set, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    ACTION_ID_ANNOTATION = "astra.openai.com/rollout-action-id"
    COMMAND_DIGEST_ANNOTATION = "astra.openai.com/rollout-command-digest"
    FENCING_TOKEN_ANNOTATION = "astra.openai.com/rollout-fencing-token"
    TARGET_REPLICAS_ANNOTATION = "astra.openai.com/rollout-target-replicas"
    CONFIG_DIGEST_ANNOTATION = "astra.openai.com/config-digest"
    EXTERNAL_ROUTING_ANNOTATION = "astra.openai.com/external-order-routing-allowed"
    LIVE_TRADING_ANNOTATION = "astra.openai.com/live-trading-allowed"

    def __post_init__(self) -> None:
        parsed = urlparse(self.api_base)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValidationErrorV107("Kubernetes API base must be credential-free HTTPS")
        if parsed.query or parsed.fragment:
            raise ValidationErrorV107("Kubernetes API base cannot contain query or fragment")
        self.api_base = self.api_base.rstrip("/")
        if not self.allowed_hosts or parsed.hostname not in set(self.allowed_hosts):
            raise ValidationErrorV107("Kubernetes API host is not allowlisted")
        if not self.cluster or not self.bearer_token:
            raise ValidationErrorV107("cluster and bearer token are required")
        if self.timeout_seconds <= 0 or self.max_response_bytes <= 0:
            raise ValidationErrorV107("invalid transport bounds")

    @staticmethod
    def _deployment_path(namespace: str, deployment_name: str) -> str:
        return (
            "/apis/apps/v1/namespaces/"
            + quote(namespace, safe="")
            + "/deployments/"
            + quote(deployment_name, safe="")
        )

    def _request(self, *, method: str, path: str, body: bytes | None, content_type: str | None = None) -> KubernetesHttpResponseV107:
        if method not in {"GET", "PATCH"}:
            raise ValidationErrorV107("unsupported Kubernetes method")
        url = self.api_base + path
        parsed = urlparse(url)
        base = urlparse(self.api_base)
        if parsed.scheme != "https" or parsed.hostname != base.hostname or parsed.hostname not in set(self.allowed_hosts):
            raise ValidationErrorV107("Kubernetes request escaped the allowlisted origin")
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.bearer_token}",
            "User-Agent": "astra-schema107-rollout-actuator/7.37.0",
        }
        if content_type is not None:
            headers["Content-Type"] = content_type
        request = KubernetesHttpRequestV107(
            method=method,
            url=url,
            headers=headers,
            body=body,
            timeout_seconds=self.timeout_seconds,
            tls_verify=True,
            allow_redirects=False,
        )
        try:
            response = self.transport(request)
        except Exception as exc:  # transport boundary intentionally normalizes all client failures
            raise KubernetesTransportErrorV107("Kubernetes transport failed") from exc
        if not isinstance(response, KubernetesHttpResponseV107):
            raise KubernetesTransportErrorV107("transport returned invalid response type")
        if len(response.body) > self.max_response_bytes:
            raise KubernetesResponseErrorV107("Kubernetes response exceeds configured size limit")
        return response

    @staticmethod
    def _expect_dict(value: Any, name: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise KubernetesResponseErrorV107(f"{name} must be an object")
        return value

    @staticmethod
    def _expect_str(value: Any, name: str) -> str:
        if not isinstance(value, str) or not value:
            raise KubernetesResponseErrorV107(f"{name} must be a non-empty string")
        return value

    @staticmethod
    def _expect_int(value: Any, name: str, *, minimum: int = 0) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise KubernetesResponseErrorV107(f"{name} must be an integer >= {minimum}")
        return value

    @staticmethod
    def _parse_bool_annotation(annotations: Mapping[str, Any], key: str) -> bool:
        value = annotations.get(key, "false")
        if value not in {"true", "false"}:
            raise KubernetesResponseErrorV107(f"annotation {key} must be true or false")
        return value == "true"

    @staticmethod
    def _parse_optional_positive_int(value: Any, name: str, *, allow_zero: bool = False) -> int | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.isdigit():
            raise KubernetesResponseErrorV107(f"{name} must be a canonical decimal string")
        parsed = int(value)
        if parsed < 0 or (parsed == 0 and not allow_zero):
            raise KubernetesResponseErrorV107(f"{name} outside allowed range")
        if str(parsed) != value:
            raise KubernetesResponseErrorV107(f"{name} is not canonical")
        return parsed

    def parse_observation(self, payload: bytes) -> KubernetesDeploymentObservationV107:
        try:
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise KubernetesResponseErrorV107("Kubernetes response is not valid UTF-8 JSON") from exc
        root = self._expect_dict(document, "deployment")
        metadata = self._expect_dict(root.get("metadata"), "metadata")
        spec = self._expect_dict(root.get("spec"), "spec")
        status_raw = root.get("status")
        status = {} if status_raw is None else self._expect_dict(status_raw, "status")
        template = self._expect_dict(spec.get("template"), "spec.template")
        template_spec = self._expect_dict(template.get("spec"), "spec.template.spec")
        containers = template_spec.get("containers")
        if not isinstance(containers, list) or len(containers) != 1 or not isinstance(containers[0], dict):
            raise KubernetesResponseErrorV107("exactly one deployment container is required")
        image = self._expect_str(containers[0].get("image"), "container.image")
        if "@sha256:" not in image:
            raise KubernetesResponseErrorV107("container image must be pinned by sha256 digest")
        image_digest = image.rsplit("@", 1)[1]

        annotations_raw = metadata.get("annotations")
        if annotations_raw is None:
            annotations_present = False
            annotations: dict[str, Any] = {}
        else:
            annotations_present = True
            annotations = self._expect_dict(annotations_raw, "metadata.annotations")
        for key, value in annotations.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise KubernetesResponseErrorV107("metadata.annotations must contain string pairs")

        config_digest = self._expect_str(annotations.get(self.CONFIG_DIGEST_ANNOTATION), "configuration digest annotation")
        action_id = annotations.get(self.ACTION_ID_ANNOTATION)
        command_digest = annotations.get(self.COMMAND_DIGEST_ANNOTATION)
        fencing_token = self._parse_optional_positive_int(
            annotations.get(self.FENCING_TOKEN_ANNOTATION),
            "fencing token annotation",
        )
        target_replicas = self._parse_optional_positive_int(
            annotations.get(self.TARGET_REPLICAS_ANNOTATION),
            "target replicas annotation",
            allow_zero=True,
        )

        snapshot = DeploymentRuntimeSnapshotV107(
            cluster=self.cluster,
            namespace=self._expect_str(metadata.get("namespace"), "metadata.namespace"),
            deployment_name=self._expect_str(metadata.get("name"), "metadata.name"),
            deployment_uid=self._expect_str(metadata.get("uid"), "metadata.uid"),
            service_account=self._expect_str(template_spec.get("serviceAccountName"), "serviceAccountName"),
            resource_version=self._expect_str(metadata.get("resourceVersion"), "metadata.resourceVersion"),
            generation=self._expect_int(metadata.get("generation"), "metadata.generation", minimum=1),
            replicas=self._expect_int(spec.get("replicas"), "spec.replicas"),
            ready_replicas=self._expect_int(status.get("readyReplicas", 0), "status.readyReplicas"),
            available_replicas=self._expect_int(status.get("availableReplicas", 0), "status.availableReplicas"),
            image_digest=image_digest,
            config_digest=config_digest,
            external_order_routing_allowed=self._parse_bool_annotation(annotations, self.EXTERNAL_ROUTING_ANNOTATION),
            live_trading_allowed=self._parse_bool_annotation(annotations, self.LIVE_TRADING_ANNOTATION),
            metadata_annotations_present=annotations_present,
            action_id_annotation=action_id,
            command_digest_annotation=command_digest,
            fencing_token_annotation=fencing_token,
            target_replicas_annotation=target_replicas,
        )
        return KubernetesDeploymentObservationV107(snapshot=snapshot, annotations=dict(annotations))

    def parse_snapshot(self, payload: bytes) -> DeploymentRuntimeSnapshotV107:
        return self.parse_observation(payload).snapshot

    def read_observation(self, *, namespace: str, deployment_name: str) -> KubernetesDeploymentObservationV107:
        response = self._request(
            method="GET",
            path=self._deployment_path(namespace, deployment_name),
            body=None,
        )
        if response.status_code != 200:
            raise KubernetesResponseErrorV107(f"Kubernetes GET returned HTTP {response.status_code}")
        return self.parse_observation(response.body)

    def read_snapshot(self, *, namespace: str, deployment_name: str) -> DeploymentRuntimeSnapshotV107:
        return self.read_observation(namespace=namespace, deployment_name=deployment_name).snapshot

    def build_patch(
        self,
        *,
        command: SignedDeploymentExecutionCommandV107,
        snapshot: DeploymentRuntimeSnapshotV107,
        current_annotations: Mapping[str, str],
    ) -> bytes:
        if not snapshot.metadata_annotations_present:
            raise ValidationErrorV107("metadata.annotations object is required for safe JSON Patch")
        if not isinstance(current_annotations, Mapping):
            raise ValidationErrorV107("current_annotations must be a mapping")
        normalized: dict[str, str] = {}
        for key, value in current_annotations.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValidationErrorV107("current_annotations must contain string pairs")
            normalized[key] = value
        normalized.update({
            self.ACTION_ID_ANNOTATION: command.intent.action_id,
            self.COMMAND_DIGEST_ANNOTATION: command.command_digest,
            self.FENCING_TOKEN_ANNOTATION: str(command.intent.fencing_token),
            self.TARGET_REPLICAS_ANNOTATION: str(command.intent.target_replicas),
        })
        operations = [
            {"op": "test", "path": "/metadata/uid", "value": command.intent.deployment_uid},
            {"op": "test", "path": "/metadata/resourceVersion", "value": command.intent.expected_resource_version},
            {"op": "test", "path": "/metadata/generation", "value": command.intent.expected_generation},
            {"op": "test", "path": "/spec/replicas", "value": command.intent.expected_current_replicas},
            {"op": "add", "path": "/metadata/annotations", "value": normalized},
            {"op": "replace", "path": "/spec/replicas", "value": command.intent.target_replicas},
        ]
        return json.dumps(operations, sort_keys=True, separators=(",", ":")).encode("utf-8")

    def apply_patch_once(
        self,
        *,
        command: SignedDeploymentExecutionCommandV107,
        patch: bytes,
    ) -> DeploymentRuntimeSnapshotV107:
        command_digest = command.command_digest
        with self._lock:
            if command_digest in self._attempted_commands:
                raise KubernetesMutationReplayV107("PATCH already attempted for command")
            self._attempted_commands.add(command_digest)
        try:
            response = self._request(
                method="PATCH",
                path=self._deployment_path(command.intent.namespace, command.intent.deployment_name),
                body=patch,
                content_type="application/json-patch+json",
            )
        except (KubernetesTransportErrorV107, KubernetesResponseErrorV107) as exc:
            raise KubernetesAmbiguousMutationV107("PATCH result is ambiguous") from exc

        if response.status_code in {409, 412}:
            raise KubernetesPreconditionFailedV107(f"Kubernetes PATCH precondition failed with HTTP {response.status_code}")
        # Only responses that prove the API server rejected the request before applying
        # it are classified as known failures. Timeouts and unknown 4xx responses are
        # ambiguous because an intermediary may have timed out after forwarding PATCH.
        if response.status_code in {400, 401, 403, 404, 405, 415, 422, 429}:
            raise KubernetesMutationRejectedV107(f"Kubernetes PATCH rejected with HTTP {response.status_code}")
        if response.status_code < 200 or response.status_code >= 300:
            raise KubernetesAmbiguousMutationV107(f"Kubernetes PATCH returned ambiguous HTTP {response.status_code}")
        try:
            return self.parse_observation(response.body).snapshot
        except KubernetesResponseErrorV107 as exc:
            raise KubernetesAmbiguousMutationV107("PATCH succeeded but response could not be verified") from exc

    @staticmethod
    def patch_digest(patch: bytes) -> str:
        return digest_v107({"content_type": "application/json-patch+json", "patch": patch.decode("utf-8")})


KUBERNETES_READ_METHODS_V107 = ("GET",)
KUBERNETES_MUTATION_METHODS_V107 = ("PATCH",)
KUBERNETES_MUTATION_ATTEMPTS_V107 = 1
TLS_VERIFY_V107 = True
ALLOW_REDIRECTS_V107 = False
