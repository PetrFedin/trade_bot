from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from urllib.parse import urlparse

import pytest

from app.runtime.deployment_qualification_v106 import ValidationErrorV106
from app.runtime.kubernetes_qualification_adapter_v106 import (
    KubernetesEndpointV106,
    KubernetesProtocolErrorV106,
    KubernetesReadOnlyAdapterV106,
    KubernetesResponseV106,
    KubernetesTransportErrorV106,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 4, 8, 0, tzinfo=UTC)
IMAGE = "sha256:" + "a" * 64
CONFIG = "sha256:" + "b" * 64
TOKEN = "kubernetes-service-account-token-0001"


def deployment_payload():
    return {
        "metadata": {
            "uid": "deploy-1",
            "generation": 7,
            "annotations": {
                "astra.openai/external-order-routing": "false",
                "astra.openai/live-trading": "false",
            },
        },
        "spec": {
            "replicas": 2,
            "selector": {"matchLabels": {"app": "astra-worker"}},
            "template": {"spec": {"serviceAccountName": "astra-worker"}},
        },
    }


def pod_payload(worker: str, zone: str, canary: bool):
    return {
        "metadata": {
            "uid": f"pod-{worker}",
            "labels": {
                "astra.openai/worker-id": worker,
                "astra.openai/canary": "true" if canary else "false",
                "topology.kubernetes.io/zone": zone,
            },
            "annotations": {
                "astra.openai/config-digest": CONFIG,
                "astra.openai/heartbeat-at": NOW.isoformat().replace("+00:00", "Z"),
                "astra.openai/certificate-not-after": (NOW + timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
                "astra.openai/active-claims": "0",
                "astra.openai/evidence-pending": "0",
                "astra.openai/broker-mutation-count": "0",
            },
        },
        "spec": {"containers": [{"name": "worker", "image": f"repo/worker@{IMAGE}"}]},
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [{"restartCount": 0}],
        },
    }


def pods_payload():
    return {"items": [pod_payload("worker-1", "zone-a", True), pod_payload("worker-2", "zone-b", False)]}


def pdb_payload():
    return {"spec": {"minAvailable": 2, "unhealthyPodEvictionPolicy": "IfHealthyBudget"}}


def network_payload():
    allowed = ",".join(sorted((
        "dns:53/tcp",
        "dns:53/udp",
        "postgresql:5432/tcp",
        "paper-api.alpaca.markets:443/tcp",
        "evidence.internal:443/tcp",
    )))
    return {
        "items": [
            {"metadata": {"annotations": {}}, "spec": {"policyTypes": ["Ingress"], "ingress": []}},
            {"metadata": {"annotations": {}}, "spec": {"policyTypes": ["Egress"], "egress": []}},
            {"metadata": {"annotations": {"astra.openai/allowed-egress": allowed}}, "spec": {"policyTypes": ["Egress"], "egress": [{"to": [{"ipBlock": {"cidr": "10.0.0.0/24"}}]}]}},
        ]
    }


class FakeTransport:
    def __init__(self, *, responses=None):
        self.responses = responses or {}
        self.calls = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        path = urlparse(kwargs["url"]).path
        query = urlparse(kwargs["url"]).query
        key = path + ("?" + query if query else "")
        response = self.responses.get(key)
        if response is None:
            raise AssertionError(f"unexpected request {key}")
        if isinstance(response, Exception):
            raise response
        if isinstance(response, KubernetesResponseV106):
            return response
        return KubernetesResponseV106(200, {"content-type": "application/json"}, json.dumps(response).encode())


def adapter(responses=None):
    endpoint = KubernetesEndpointV106("https://kube.internal", ("kube.internal",), "astra", TOKEN)
    if responses is None:
        responses = {
            "/apis/apps/v1/namespaces/astra/deployments/astra-worker": deployment_payload(),
            "/api/v1/namespaces/astra/pods?labelSelector=app=astra-worker": pods_payload(),
            "/apis/policy/v1/namespaces/astra/poddisruptionbudgets/astra-worker": pdb_payload(),
            "/apis/networking.k8s.io/v1/namespaces/astra/networkpolicies": network_payload(),
        }
    transport = FakeTransport(responses=responses)
    return KubernetesReadOnlyAdapterV106(endpoint, transport), transport


def test_collect_snapshot_uses_strict_read_only_transport_options():
    client, transport = adapter()
    snapshot = client.collect_snapshot(deployment_name="astra-worker", pdb_name="astra-worker", cluster="cluster-1", observed_at=NOW)
    assert snapshot.deployment_id == "deploy-1"
    assert snapshot.available_replicas == 2
    assert snapshot.canary_ready_replicas == 1
    assert dict(snapshot.zone_replicas) == {"zone-a": 1, "zone-b": 1}
    assert snapshot.network_policy.default_deny_ingress
    assert snapshot.network_policy.default_deny_egress
    assert len(transport.calls) == 4
    for call in transport.calls:
        assert call["method"] == "GET"
        assert call["tls_verify"] is True
        assert call["allow_redirects"] is False
        assert call["timeout_seconds"] == 5.0
        assert call["max_response_bytes"] == 2_000_000
        assert call["headers"]["Authorization"].startswith("Bearer ")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"api_base": "http://kube.internal"},
        {"api_base": "https://other.internal"},
        {"api_base": "https://kube.internal/path"},
        {"namespace": "../astra"},
        {"bearer_token": "short"},
        {"timeout_seconds": 0},
        {"timeout_seconds": 31},
        {"max_response_bytes": 0},
        {"max_response_bytes": 11_000_000},
    ],
)
def test_endpoint_rejects_unsafe_configuration(kwargs):
    values = dict(api_base="https://kube.internal", allowed_hosts=("kube.internal",), namespace="astra", bearer_token=TOKEN)
    values.update(kwargs)
    with pytest.raises(ValidationErrorV106):
        KubernetesEndpointV106(**values)


@pytest.mark.parametrize(
    "response,error",
    [
        (KubernetesResponseV106(302, {"content-type": "application/json"}, b"{}"), KubernetesTransportErrorV106),
        (KubernetesResponseV106(500, {"content-type": "application/json"}, b"{}"), KubernetesTransportErrorV106),
        (KubernetesResponseV106(200, {"content-type": "text/plain"}, b"{}"), KubernetesProtocolErrorV106),
        (KubernetesResponseV106(200, {"content-type": "application/json"}, b"not-json"), KubernetesProtocolErrorV106),
        (KubernetesResponseV106(200, {"content-type": "application/json"}, b"[]"), KubernetesProtocolErrorV106),
    ],
)
def test_get_json_rejects_bad_responses(response, error):
    client, _ = adapter({"/apis/apps/v1/namespaces/astra/deployments/astra-worker": response})
    with pytest.raises(error):
        client._get_json("/apis/apps/v1/namespaces/astra/deployments/astra-worker")


def test_get_json_rejects_oversized_response():
    endpoint = KubernetesEndpointV106("https://kube.internal", ("kube.internal",), "astra", TOKEN, max_response_bytes=4)
    response = KubernetesResponseV106(200, {"content-type": "application/json"}, b'{"x":1}')
    client = KubernetesReadOnlyAdapterV106(endpoint, FakeTransport(responses={"/apis/apps/v1/namespaces/astra/deployments/x": response}))
    with pytest.raises(KubernetesTransportErrorV106):
        client._get_json("/apis/apps/v1/namespaces/astra/deployments/x")


@pytest.mark.parametrize("path", ["relative", "/api/../secrets", "/api//v1", "/api/v1/namespaces/astra/secrets"])
def test_get_json_rejects_paths_outside_allowlist(path):
    client, _ = adapter()
    with pytest.raises(KubernetesProtocolErrorV106):
        client._get_json(path)


def test_collect_snapshot_rejects_invalid_names():
    client, _ = adapter()
    with pytest.raises(ValidationErrorV106):
        client.collect_snapshot(deployment_name="../x", pdb_name="x", cluster="c", observed_at=NOW)


def test_collect_snapshot_requires_selector():
    responses = {
        "/apis/apps/v1/namespaces/astra/deployments/astra-worker": {"metadata": {}, "spec": {}},
    }
    client, _ = adapter(responses)
    with pytest.raises(KubernetesProtocolErrorV106):
        client.collect_snapshot(deployment_name="astra-worker", pdb_name="astra-worker", cluster="cluster-1", observed_at=NOW)


@pytest.mark.parametrize(
    "mutator",
    [
        lambda p: p["items"].__setitem__(0, "bad"),
        lambda p: p["items"][0]["spec"].__setitem__("containers", []),
        lambda p: p["items"][0]["spec"]["containers"][0].__setitem__("image", "repo/latest"),
        lambda p: p["items"][0]["metadata"]["annotations"].__setitem__("astra.openai/heartbeat-at", "bad"),
        lambda p: p["items"][0]["metadata"]["annotations"].__setitem__("astra.openai/active-claims", "bad"),
        lambda p: p["items"][0]["metadata"]["labels"].__setitem__("astra.openai/worker-id", "worker-2"),
    ],
)
def test_collect_snapshot_rejects_malformed_pods(mutator):
    pods = pods_payload()
    mutator(pods)
    responses = {
        "/apis/apps/v1/namespaces/astra/deployments/astra-worker": deployment_payload(),
        "/api/v1/namespaces/astra/pods?labelSelector=app=astra-worker": pods,
        "/apis/policy/v1/namespaces/astra/poddisruptionbudgets/astra-worker": pdb_payload(),
        "/apis/networking.k8s.io/v1/namespaces/astra/networkpolicies": network_payload(),
    }
    client, _ = adapter(responses)
    with pytest.raises((KubernetesProtocolErrorV106, ValidationErrorV106)):
        client.collect_snapshot(deployment_name="astra-worker", pdb_name="astra-worker", cluster="cluster-1", observed_at=NOW)


def test_collect_snapshot_rejects_invalid_boolean_annotation():
    deployment = deployment_payload()
    deployment["metadata"]["annotations"]["astra.openai/live-trading"] = "yes"
    responses = {
        "/apis/apps/v1/namespaces/astra/deployments/astra-worker": deployment,
        "/api/v1/namespaces/astra/pods?labelSelector=app=astra-worker": pods_payload(),
        "/apis/policy/v1/namespaces/astra/poddisruptionbudgets/astra-worker": pdb_payload(),
        "/apis/networking.k8s.io/v1/namespaces/astra/networkpolicies": network_payload(),
    }
    client, _ = adapter(responses)
    with pytest.raises(KubernetesProtocolErrorV106):
        client.collect_snapshot(deployment_name="astra-worker", pdb_name="astra-worker", cluster="cluster-1", observed_at=NOW)


def test_network_policy_detects_broad_cidr_and_live_host():
    payload = network_payload()
    payload["items"][2]["metadata"]["annotations"]["astra.openai/live-hosts"] = "api.alpaca.markets"
    payload["items"][2]["spec"]["egress"][0]["to"][0]["ipBlock"]["cidr"] = "0.0.0.0/0"
    snapshot = KubernetesReadOnlyAdapterV106._normalize_network_policy(payload)
    assert snapshot.broad_cidrs == ("0.0.0.0/0",)
    assert snapshot.live_hosts == ("api.alpaca.markets",)


def test_network_policy_rejects_non_list_items_and_bad_annotations():
    with pytest.raises(KubernetesProtocolErrorV106):
        KubernetesReadOnlyAdapterV106._normalize_network_policy({"items": {}})
    with pytest.raises(KubernetesProtocolErrorV106):
        KubernetesReadOnlyAdapterV106._normalize_network_policy({"items": [{"metadata": {"annotations": []}, "spec": {}}]})


def test_pdb_rejects_percentage_and_accepts_max_unavailable():
    with pytest.raises(KubernetesProtocolErrorV106):
        KubernetesReadOnlyAdapterV106._normalize_pdb({"spec": {"minAvailable": "50%"}})
    pdb = KubernetesReadOnlyAdapterV106._normalize_pdb({"spec": {"maxUnavailable": 1, "unhealthyPodEvictionPolicy": "AlwaysAllow"}})
    assert pdb.max_unavailable == 1


def test_adapter_remaining_parser_branches():
    client, _ = adapter()
    with pytest.raises(KubernetesProtocolErrorV106):
        client._parse_time("", "time")
    with pytest.raises(KubernetesProtocolErrorV106):
        client._parse_time("2026-01-01T00:00:00", "time")
    with pytest.raises(KubernetesProtocolErrorV106):
        client._int_annotation({"x": "-1"}, "x")
    with pytest.raises(KubernetesProtocolErrorV106):
        client._restart_count({"status": {"containerStatuses": {}}})
    normalized = client._normalize_network_policy({"items": ["ignored", {"metadata": {}, "spec": {"egress": ["ignored", {"to": ["ignored"]}]}}]})
    assert normalized.allowed_egress == ()


def test_adapter_detects_origin_escape_after_endpoint_tamper():
    client, _ = adapter()
    object.__setattr__(client._endpoint, "api_base", "https://evil.internal")
    with pytest.raises(KubernetesProtocolErrorV106):
        client._get_json("/apis/apps/v1/namespaces/astra/deployments/astra-worker")
