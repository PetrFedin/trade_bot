from __future__ import annotations

from dataclasses import replace
import json

import pytest

from app.runtime.kubernetes_rollout_adapter_v107 import (
    KubernetesAdapterErrorV107,
    KubernetesAmbiguousMutationV107,
    KubernetesHttpResponseV107,
    KubernetesMutationRejectedV107,
    KubernetesMutationReplayV107,
    KubernetesPreconditionFailedV107,
    KubernetesResponseErrorV107,
    KubernetesRolloutAdapterV107,
    KubernetesTransportErrorV107,
)
from app.runtime.rollout_execution_v107 import ValidationErrorV107
from tests.conftest import CONFIG, IMAGE, deployment_document


class RecordingTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if not self.responses:
            raise RuntimeError("no response")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def adapter(transport):
    return KubernetesRolloutAdapterV107(
        api_base="https://k8s.example.internal",
        cluster="cluster-a",
        bearer_token="token",
        transport=transport,
        allowed_hosts=("k8s.example.internal",),
        timeout_seconds=3,
        max_response_bytes=100_000,
    )


def test_requires_allowlisted_https_origin():
    with pytest.raises(ValidationErrorV107):
        KubernetesRolloutAdapterV107(
            api_base="http://k8s.example.internal",
            cluster="cluster-a",
            bearer_token="token",
            transport=lambda _: None,
            allowed_hosts=("k8s.example.internal",),
        )
    with pytest.raises(ValidationErrorV107, match="allowlisted"):
        KubernetesRolloutAdapterV107(
            api_base="https://evil.example.internal",
            cluster="cluster-a",
            bearer_token="token",
            transport=lambda _: None,
            allowed_hosts=("k8s.example.internal",),
        )


def test_get_is_tls_verified_redirect_free_and_exact_origin():
    transport = RecordingTransport([KubernetesHttpResponseV107(200, deployment_document())])
    snap = adapter(transport).read_snapshot(namespace="astra-prod", deployment_name="trade-bot-workers")
    assert snap.replicas == 2
    request = transport.requests[0]
    assert request.method == "GET"
    assert request.tls_verify is True
    assert request.allow_redirects is False
    assert request.url == "https://k8s.example.internal/apis/apps/v1/namespaces/astra-prod/deployments/trade-bot-workers"
    assert request.headers["Authorization"] == "Bearer token"


def test_path_segments_are_escaped():
    transport = RecordingTransport([KubernetesHttpResponseV107(404, b"{}")])
    with pytest.raises(KubernetesResponseErrorV107):
        adapter(transport).read_snapshot(namespace="a/b", deployment_name="x y")
    assert "/a%2Fb/deployments/x%20y" in transport.requests[0].url


def test_parse_rejects_non_object_annotations():
    doc = json.loads(deployment_document())
    doc["metadata"]["annotations"] = []
    with pytest.raises(KubernetesResponseErrorV107, match="annotations"):
        adapter(RecordingTransport([])).parse_snapshot(json.dumps(doc).encode())


def test_parse_rejects_multiple_containers():
    doc = json.loads(deployment_document())
    doc["spec"]["template"]["spec"]["containers"].append({"image": "x@" + IMAGE})
    with pytest.raises(KubernetesResponseErrorV107, match="exactly one"):
        adapter(RecordingTransport([])).parse_snapshot(json.dumps(doc).encode())


def test_parse_rejects_unpinned_image():
    doc = json.loads(deployment_document())
    doc["spec"]["template"]["spec"]["containers"][0]["image"] = "latest"
    with pytest.raises(KubernetesResponseErrorV107, match="pinned"):
        adapter(RecordingTransport([])).parse_snapshot(json.dumps(doc).encode())


def test_parse_rejects_invalid_boolean_annotation():
    doc = json.loads(deployment_document())
    doc["metadata"]["annotations"]["astra.openai.com/live-trading-allowed"] = "FALSE"
    with pytest.raises(KubernetesResponseErrorV107, match="true or false"):
        adapter(RecordingTransport([])).parse_snapshot(json.dumps(doc).encode())


def test_parse_rejects_partial_execution_marker():
    doc = json.loads(deployment_document(annotations={
        "astra.openai.com/rollout-action-id": "action-001",
    }))
    with pytest.raises(ValidationErrorV107, match="entirely absent or complete"):
        adapter(RecordingTransport([])).parse_snapshot(json.dumps(doc).encode())


def test_parse_rejects_noncanonical_numeric_marker():
    doc = json.loads(deployment_document(annotations={
        "astra.openai.com/rollout-action-id": "action-001",
        "astra.openai.com/rollout-command-digest": "a" * 64,
        "astra.openai.com/rollout-fencing-token": "011",
        "astra.openai.com/rollout-target-replicas": "4",
    }))
    with pytest.raises(KubernetesResponseErrorV107, match="canonical"):
        adapter(RecordingTransport([])).parse_snapshot(json.dumps(doc).encode())


def test_missing_annotations_is_explicitly_not_safe():
    doc = json.loads(deployment_document())
    doc["metadata"].pop("annotations")
    with pytest.raises(KubernetesResponseErrorV107, match="configuration digest"):
        adapter(RecordingTransport([])).parse_snapshot(json.dumps(doc).encode())


def test_build_patch_has_exact_preconditions_and_markers(command, snapshot):
    a = adapter(RecordingTransport([]))
    patch = json.loads(a.build_patch(
        command=command,
        snapshot=snapshot,
        current_annotations={
            "astra.openai.com/config-digest": CONFIG,
            "other": "keep",
        },
    ))
    assert patch[0] == {"op": "test", "path": "/metadata/uid", "value": "uid-123"}
    assert {"op": "test", "path": "/metadata/resourceVersion", "value": "100"} in patch
    assert {"op": "test", "path": "/metadata/generation", "value": 7} in patch
    assert {"op": "test", "path": "/spec/replicas", "value": 2} in patch
    annotations = next(op["value"] for op in patch if op["path"] == "/metadata/annotations")
    assert annotations["other"] == "keep"
    assert annotations[a.COMMAND_DIGEST_ANNOTATION] == command.command_digest
    assert patch[-1] == {"op": "replace", "path": "/spec/replicas", "value": 4}


def test_build_patch_rejects_missing_annotations_object(command, snapshot):
    with pytest.raises(ValidationErrorV107, match="annotations"):
        adapter(RecordingTransport([])).build_patch(
            command=command,
            snapshot=replace(snapshot, metadata_annotations_present=False),
            current_annotations={},
        )


def test_patch_success_and_single_attempt(command, snapshot):
    markers = {
        "astra.openai.com/rollout-action-id": command.intent.action_id,
        "astra.openai.com/rollout-command-digest": command.command_digest,
        "astra.openai.com/rollout-fencing-token": "11",
        "astra.openai.com/rollout-target-replicas": "4",
    }
    transport = RecordingTransport([KubernetesHttpResponseV107(200, deployment_document(replicas=4, ready=4, available=4, annotations=markers))])
    a = adapter(transport)
    patch = a.build_patch(command=command, snapshot=snapshot, current_annotations={"astra.openai.com/config-digest": CONFIG})
    post = a.apply_patch_once(command=command, patch=patch)
    assert post.replicas == 4
    assert transport.requests[0].method == "PATCH"
    assert transport.requests[0].headers["Content-Type"] == "application/json-patch+json"
    with pytest.raises(KubernetesMutationReplayV107):
        a.apply_patch_once(command=command, patch=patch)


@pytest.mark.parametrize("status", [409, 412])
def test_patch_precondition_failure_is_known(command, snapshot, status):
    a = adapter(RecordingTransport([KubernetesHttpResponseV107(status, b"{}")]))
    patch = a.build_patch(command=command, snapshot=snapshot, current_annotations={"astra.openai.com/config-digest": CONFIG})
    with pytest.raises(KubernetesPreconditionFailedV107):
        a.apply_patch_once(command=command, patch=patch)


@pytest.mark.parametrize("status", [400, 403, 404, 422])
def test_patch_other_4xx_is_rejected(command, snapshot, status):
    a = adapter(RecordingTransport([KubernetesHttpResponseV107(status, b"{}")]))
    patch = a.build_patch(command=command, snapshot=snapshot, current_annotations={"astra.openai.com/config-digest": CONFIG})
    with pytest.raises(KubernetesMutationRejectedV107):
        a.apply_patch_once(command=command, patch=patch)


@pytest.mark.parametrize("response", [
    KubernetesHttpResponseV107(408, b"{}"),
    KubernetesHttpResponseV107(500, b"{}"),
    KubernetesHttpResponseV107(200, b"not-json"),
    RuntimeError("timeout"),
])
def test_patch_ambiguous_outcomes_are_never_retried(command, snapshot, response):
    a = adapter(RecordingTransport([response]))
    patch = a.build_patch(command=command, snapshot=snapshot, current_annotations={"astra.openai.com/config-digest": CONFIG})
    with pytest.raises(KubernetesAmbiguousMutationV107):
        a.apply_patch_once(command=command, patch=patch)
    with pytest.raises(KubernetesMutationReplayV107):
        a.apply_patch_once(command=command, patch=patch)


def test_get_transport_error_is_not_misclassified_as_patch():
    a = adapter(RecordingTransport([RuntimeError("down")]))
    with pytest.raises(KubernetesTransportErrorV107):
        a.read_snapshot(namespace="astra-prod", deployment_name="trade-bot-workers")


def test_response_size_limit():
    a = KubernetesRolloutAdapterV107(
        api_base="https://k8s.example.internal", cluster="cluster-a", bearer_token="token",
        transport=RecordingTransport([KubernetesHttpResponseV107(200, b"x" * 11)]),
        allowed_hosts=("k8s.example.internal",), max_response_bytes=10,
    )
    with pytest.raises(KubernetesResponseErrorV107, match="size"):
        a.read_snapshot(namespace="n", deployment_name="d")
