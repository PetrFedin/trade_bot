import json
import re
from pathlib import Path

STATUS_PATH = Path("CURRENT_SYSTEM_STATUS.json")
SHA40 = re.compile(r"^[0-9a-f]{40}$")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_status() -> dict:
    return load_json(STATUS_PATH)


def test_current_system_status_is_generated_fail_closed_and_not_profitable() -> None:
    status = load_status()

    assert status["schema_version"] == "current-system-status-v3"
    assert status["authority"]["generator"] == "tools/system_status.py"
    assert status["authority"]["tracked_status_is_generated"] is True
    assert status["authority"]["readme_status_block_is_generated"] is True

    repository = status["repository"]
    assert repository["observed_checkout_sha"] == "<runtime:git-rev-parse-head>"
    assert repository["checkout_relation"] == "<runtime:computed>"
    assert repository["latest_engineering_qualified_main_sha"] == (
        "c50ea8794843050e0bf6baec6026f2ddd1421c9f"
    )
    assert repository["engineering_qualification_scope"] == "ENGINEERING_CI_ONLY"

    strategy = status["strategy"]
    assert strategy["status"] == "PROFITABILITY_NOT_PROVEN"
    assert strategy["promotion_allowed"] is False
    replay = strategy["latest_frozen_bybit_price_only_replay"]
    assert float(replay["net_pnl_usdt"]) < 0
    assert float(replay["net_return_fraction_approx"]) < 0

    authority = status["trading_authority"]
    assert authority["external_order_routing_allowed"] is False
    assert authority["demo_trading_allowed"] is False
    assert authority["live_trading_allowed"] is False
    assert authority["mainnet_entry_allowed"] is False
    assert authority["production_release_allowed"] is False


def test_engineering_qualification_manifest_is_exact_and_cannot_promote() -> None:
    status = load_status()
    relative = status["repository"]["latest_engineering_qualification_manifest"]
    manifest = load_json(Path(relative))

    assert manifest["schema_version"] == "engineering-main-qualification-v1"
    assert SHA40.fullmatch(manifest["subject_sha"])
    assert manifest["subject_sha"] == (
        status["repository"]["latest_engineering_qualified_main_sha"]
    )
    assert manifest["scope"] == "ENGINEERING_CI_ONLY"
    assert manifest["source_pull_request"] == 182
    assert manifest["github"]["workflow_run_count"] == 6
    assert manifest["github"]["check_run_count"] == 7
    assert manifest["github"]["all_observed_workflows_success"] is True
    assert manifest["github"]["all_observed_check_runs_success"] is True
    assert "F22D_FENCED_OPERATIONAL_DECISION_WORKERS" in manifest[
        "qualified_capabilities"
    ]
    assert "MAIN_BRANCH_PROTECTION_OBSERVED_ENABLED_PARTIAL" in manifest[
        "qualified_capabilities"
    ]
    assert all(value is False for value in manifest["promotion"].values())


def test_stale_c2_current_state_is_not_republished_as_current_truth() -> None:
    status = load_status()
    stale_current_keys = {
        "canonical_main",
        "canonical_operational_foundation",
        "canonical_database_security",
        "canonical_append_only_audit",
        "canonical_v120_persistence",
        "canonical_v119_excursion_persistence",
        "next_gate_candidate",
        "consolidation",
        "operational_source_boundary",
        "research_head",
    }

    assert stale_current_keys.isdisjoint(status)


def test_next_vertical_gate_is_server_side_main_protection_enforcement() -> None:
    status = load_status()
    gate = status["engineering_qualification"]["next_vertical_gate"]

    assert gate["issue"] == 103
    assert gate["name"] == "SERVER_SIDE_MAIN_PROTECTION_ENFORCEMENT"
    assert gate["required_families"] == [
        "REQUIRED_STATUS_CHECKS",
        "PR_REVIEW_ENFORCEMENT",
        "BYPASS_AND_FORCE_PUSH_POLICY",
    ]


def test_frozen_strategy_evidence_is_explicitly_carried_forward_and_negative() -> None:
    status = load_status()
    evidence = load_json(Path(status["strategy"]["evidence_source"]))

    assert evidence["evidence_kind"] == "CARRIED_FORWARD_FROZEN_NEGATIVE_RESULT"
    assert evidence["strategy_status"] == "PROFITABILITY_NOT_PROVEN"
    assert evidence["promotion_allowed"] is False
    assert SHA40.fullmatch(evidence["source_repository_sha"])
    assert evidence["portfolio_trades"] == 102
    assert evidence["wins"] == 36
    assert evidence["breakeven"] == 11
    assert evidence["losses"] == 55
    assert float(evidence["net_pnl_usdt"]) < 0


def test_release_authority_remains_source_ci_only_and_fail_closed() -> None:
    status = load_status()
    live_path = Path(status["release"]["live_execution_status_source"])
    live = load_json(live_path)

    assert status["release"]["schema"] == 109
    assert status["release"]["version"] == "7.39.0"
    assert status["release"]["status"] == "SOURCE_AND_CI_QUALIFICATION_ONLY"
    assert live["status"] == "SOURCE_AND_CI_QUALIFICATION_ONLY"
    assert live["external_order_routing_allowed"] is False
    assert live["live_trading_allowed"] is False
