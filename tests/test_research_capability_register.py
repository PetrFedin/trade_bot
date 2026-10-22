"""Fail-closed capability comparison registry contract tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tools.research_capability_register import (
    REGISTRY,
    ROOT,
    CapabilityRegisterError,
    read_validate_register,
    validate_register,
)


def _case():
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


def test_real_registry_has_paths_and_closed_authority():
    result = read_validate_register()
    assert result["feature_count"] >= 10
    assert result["counts_by_state"]["IMPLEMENTED_PENDING_QUALIFICATION"] == 2
    assert result["counts_by_state"]["BLOCKED_SOURCE"] == 1
    assert result["all_authorities_closed"] is True
    assert result["commercial_feature_parity_proven"] is False
    assert result["real_trading_ready"] is False


@pytest.mark.parametrize("field", [
    "broker_routing_allowed", "demo_trading_allowed", "live_trading_allowed",
    "strategy_promotion_allowed", "profitability_proven",
])
def test_authority_flag_tampering_rejected(field):
    data = _case()
    data["authority_boundary"][field] = True
    with pytest.raises(CapabilityRegisterError, match="authorize trading"):
        validate_register(data, ROOT)


def test_capability_level_authority_rejected():
    data = _case()
    data["capabilities"][0]["trading_authority_allowed"] = True
    with pytest.raises(CapabilityRegisterError, match="cannot grant"):
        validate_register(data, ROOT)


def test_fake_qualified_state_rejected():
    data = _case()
    data["capabilities"][0]["state"] = "LIVE_READY"
    with pytest.raises(CapabilityRegisterError, match="unsupported readiness"):
        validate_register(data, ROOT)


def test_missing_evidence_path_rejected():
    data = _case()
    data["capabilities"][0]["code_paths"].append("tools/does_not_exist.py")
    with pytest.raises(CapabilityRegisterError, match="missing capability evidence"):
        validate_register(data, ROOT)


def test_planned_feature_cannot_claim_code_evidence():
    data = _case()
    planned = next(x for x in data["capabilities"] if x["state"] == "PLANNED")
    planned["code_paths"] = ["tools/research_signal_gate.py"]
    with pytest.raises(CapabilityRegisterError, match="planned capability"):
        validate_register(data, ROOT)


def test_missing_and_cyclic_feature_dependencies_rejected():
    data = _case()
    data["capabilities"][0]["depends_on"] = ["NONEXISTENT_FEATURE"]
    with pytest.raises(CapabilityRegisterError, match="missing feature"):
        validate_register(data, ROOT)
    data = _case()
    by_id = {row["id"]: row for row in data["capabilities"]}
    by_id["ACCOUNT_TRUTH_RECONCILIATION"]["depends_on"] = ["PORTFOLIO_RISK_SCENARIOS"]
    with pytest.raises(CapabilityRegisterError, match="cycle"):
        validate_register(data, ROOT)


def test_untrusted_competitor_reference_rejected():
    data = _case()
    data["capabilities"][0]["official_references"] = [
        "https://evil.example/fake-feature",
    ]
    with pytest.raises(CapabilityRegisterError, match="unapproved"):
        validate_register(data, ROOT)


def test_duplicate_ids_rejected():
    data = _case()
    data["capabilities"][1]["id"] = data["capabilities"][0]["id"]
    with pytest.raises(CapabilityRegisterError, match="duplicate"):
        validate_register(data, ROOT)


def test_manifest_parser_rejects_duplicate_json_keys(tmp_path):
    target = tmp_path / "register.json"
    target.write_text('{"schema_version":"a","schema_version":"b"}')
    with pytest.raises(CapabilityRegisterError, match="duplicate"):
        read_validate_register(target, ROOT)


def test_manifest_rejects_symlink_path(tmp_path):
    protected = tmp_path / "protected.json"
    protected.write_text(json.dumps(_case()), encoding="utf-8")
    target = tmp_path / "register.json"
    target.symlink_to(protected)
    with pytest.raises(CapabilityRegisterError, match="untrusted"):
        read_validate_register(target, ROOT)


def test_not_allowed_to_create_qualifications_from_file_existence():
    data = copy.deepcopy(_case())
    assert any(row["state"] == "IMPLEMENTED_PENDING_QUALIFICATION"
               for row in data["capabilities"])
    result = validate_register(data, ROOT)
    assert result["real_trading_ready"] is False
    assert "QUALIFIED" not in result["counts_by_state"]


def test_unsafe_path_rejected():
    data = _case()
    data["capabilities"][0]["code_paths"] = ["../outside.py"]
    with pytest.raises(CapabilityRegisterError, match="unsafe"):
        validate_register(data, ROOT)


def test_registry_source_is_inside_repository():
    assert isinstance(REGISTRY, Path)
    assert REGISTRY.parent == ROOT / "research"
