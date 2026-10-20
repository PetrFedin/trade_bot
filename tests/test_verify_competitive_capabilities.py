"""Negative assurance for source-attributed competitor and ASTRA capability claims."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.verify_competitive_capabilities import (
    CompetitiveEvidenceError,
    read_and_validate,
    validate_ledger,
)

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "research/competitive_capabilities_2026-10-20.json"


def _data():
    return json.loads(LEDGER.read_text(encoding="utf-8"))


def _feature(data, name):
    return next(row for row in data["capabilities"] if row["id"] == name)


def test_actual_inventory_passes_without_trading_claims():
    receipt = read_and_validate(LEDGER, ROOT)
    assert receipt["competitors"] == 11
    assert receipt["capabilities"] == 31
    assert receipt["status_counts"] == {
        "BLOCKED_SOURCE": 1,
        "ENGINEERING_ONLY": 8,
        "NOT_IMPLEMENTED": 15,
        "RESEARCH_ONLY": 7,
    }
    assert receipt["result"] == "PASS_SOURCE_ATTRIBUTION_AND_PATH_INVENTORY_ONLY"
    assert receipt["hands_on_competitor_qa_verified"] is False
    assert receipt["feature_runtime_parity_verified"] is False
    assert receipt["broker_execution_verified"] is False
    assert receipt["strategy_promotion_allowed"] is False
    assert receipt["live_trading_allowed"] is False
    assert receipt["profitability_proven"] is False
    assert len(receipt["ledger_sha256"]) == 64


@pytest.mark.parametrize("permission", [
    "external_order_routing_allowed",
    "demo_trading_allowed",
    "live_trading_allowed",
    "mainnet_entry_allowed",
    "production_release_allowed",
    "strategy_promotion_allowed",
    "profitability_proven",
])
def test_capability_inventory_never_grants_execution_permission(permission):
    data = _data()
    data["authority"][permission] = True
    with pytest.raises(CompetitiveEvidenceError, match="cannot grant trading permission"):
        validate_ledger(data, ROOT)


def test_duplicate_capability_fails_closed():
    data = _data()
    data["capabilities"].append(dict(data["capabilities"][0]))
    with pytest.raises(CompetitiveEvidenceError, match="duplicate capability"):
        validate_ledger(data, ROOT)


def test_duplicate_competitor_fails_closed():
    data = _data()
    data["competitors"].append(dict(data["competitors"][0]))
    with pytest.raises(CompetitiveEvidenceError, match="duplicate competitor"):
        validate_ledger(data, ROOT)


def test_unapproved_official_domain_fails():
    data = _data()
    item = next(c for c in data["competitors"] if c["id"] == "cryptohopper")
    item["official_urls"] = ["https://unrelated.invalid/features"]
    with pytest.raises(CompetitiveEvidenceError, match="unapproved or malformed"):
        validate_ledger(data, ROOT)


def test_unknown_competitor_cannot_be_cited():
    data = _data()
    feature = _feature(data, "signal_webhook_gateway")
    feature["competitor_ids"] = sorted(feature["competitor_ids"] + ["unknown_vendor"])
    with pytest.raises(CompetitiveEvidenceError, match="unrecognized competitor"):
        validate_ledger(data, ROOT)


def test_wrong_competitor_url_is_not_source_evidence():
    data = _data()
    feature = _feature(data, "signal_webhook_gateway")
    feature["source_urls"] = sorted(feature["source_urls"] + [
        "https://www.cryptohopper.com/features"
    ])
    with pytest.raises(CompetitiveEvidenceError, match="citation missing"):
        validate_ledger(data, ROOT)


def test_unbuilt_feature_cannot_claim_implementation():
    data = _data()
    feature = _feature(data, "visual_strategy_builder")
    feature["evidence_paths"] = ["app/strategy/position_management.py"]
    with pytest.raises(CompetitiveEvidenceError, match="unbuilt feature"):
        validate_ledger(data, ROOT)


def test_engineering_claim_requires_existing_code():
    data = _data()
    feature = _feature(data, "portfolio_risk_admission")
    feature["evidence_paths"] = ["tools/not_a_real_module.py"]
    with pytest.raises(CompetitiveEvidenceError, match="missing or symlinked"):
        validate_ledger(data, ROOT)


def test_blocked_mark_price_cannot_be_auto_promoted():
    data = _data()
    feature = _feature(data, "funding_mark_source")
    feature["build_decision"] = "BUILD"
    with pytest.raises(CompetitiveEvidenceError, match="blocked provider"):
        validate_ledger(data, ROOT)


def test_unknown_capability_status_cannot_claim_runtime():
    data = _data()
    feature = _feature(data, "historical_backtests")
    feature["astra_status"] = "PROVEN_LIVE"
    with pytest.raises(CompetitiveEvidenceError, match="unsupported implementation status"):
        validate_ledger(data, ROOT)


def test_malformed_snapshot_cannot_be_used():
    data = _data()
    data["snapshot_date"] = "2026-99-99"
    with pytest.raises(CompetitiveEvidenceError, match="invalid snapshot date"):
        validate_ledger(data, ROOT)


def test_duplicate_json_keys_rejected(tmp_path):
    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"schema_version":"a","schema_version":"b"}')
    with pytest.raises(CompetitiveEvidenceError, match="duplicate JSON key"):
        read_and_validate(invalid, ROOT)


def test_symlinked_ledger_is_rejected(tmp_path):
    link = tmp_path / "link.json"
    link.symlink_to(LEDGER)
    with pytest.raises(CompetitiveEvidenceError, match="missing ledger source"):
        read_and_validate(link, ROOT)


def test_paths_must_remain_inside_authoritative_repository():
    data = _data()
    feature = _feature(data, "historical_backtests")
    feature["evidence_paths"] = ["../outside-repository.txt"]
    with pytest.raises(CompetitiveEvidenceError, match="unsafe evidence path"):
        validate_ledger(data, ROOT)


def test_source_references_require_actual_competitor():
    data = _data()
    feature = _feature(data, "investor_readiness_dossier")
    feature["source_urls"] = ["https://www.cryptohopper.com/features"]
    with pytest.raises(CompetitiveEvidenceError, match="citation missing"):
        validate_ledger(data, ROOT)
