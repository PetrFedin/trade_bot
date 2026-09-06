import json
import re
from pathlib import Path

STATUS_PATH = Path("CURRENT_SYSTEM_STATUS.json")
SHA40 = re.compile(r"^[0-9a-f]{40}$")


def load_status() -> dict:
    return json.loads(STATUS_PATH.read_text(encoding="utf-8"))


def test_current_system_status_is_current_fail_closed_and_not_profitable() -> None:
    status = load_status()

    assert status["schema_version"] == "current-system-status-v2"
    assert status["observed_at"] == "2026-09-06"

    main = status["canonical_main"]
    assert main["last_qualified_sha"] == (
        "043fc3003b055dc1e953854798627048d3f26960"
    )
    assert main["engineering_baseline_status"] == "PASS"
    assert main["qualification"]["post_merge_workflows_completed"] == 11
    assert main["qualification"]["post_merge_workflows_success"] == 11
    assert main["qualification"]["focused_security_regression_result"] == "282 passed"
    assert main["qualification"]["full_regression_result"] == (
        "1124 passed, 2 dedicated fleet-deployment skips"
    )
    assert main["qualification"]["postgres_full_regression_enabled"] is True
    assert main["qualification"]["postgres_version"] == "16.15"
    assert main["qualification"]["release_attestation"] == (
        "SLSA_AND_SBOM_SIGNED_PASS"
    )

    strategy = status["strategy"]
    assert strategy["status"] == "PROFITABILITY_NOT_PROVEN"
    assert strategy["promotion_allowed"] is False
    replay = strategy["latest_frozen_bybit_price_only_replay"]
    assert float(replay["net_pnl_usdt"]) < 0

    live = status["live"]
    assert live["status"] == "FAIL_CLOSED"
    assert live["external_order_routing_allowed"] is False
    assert live["live_trading_allowed"] is False
    assert live["mainnet_entry_allowed"] is False
    assert live["production_release_allowed"] is False


def test_c2a0_through_c2a3_remain_qualified_historical_components() -> None:
    status = load_status()

    c2a0 = status["canonical_operational_foundation"]
    assert c2a0["id"] == "C2A0"
    assert c2a0["status"] == "EXTRACTED_AND_QUALIFIED"
    assert c2a0["replacement_pr"] == 113
    assert c2a0["research_ancestry_inherited"] is False
    assert c2a0["order_write_capability"] is False

    c2a1 = status["canonical_database_security"]
    assert c2a1["id"] == "C2A1"
    assert c2a1["status"] == "EXTRACTED_AND_QUALIFIED"
    assert c2a1["runtime_role_must_be_non_owner"] is True
    assert c2a1["truncate_or_ddl_authority_allowed"] is False

    c2a2 = status["canonical_append_only_audit"]
    assert c2a2["id"] == "C2A2"
    assert c2a2["status"] == "EXTRACTED_AND_QUALIFIED"
    assert c2a2["forward_truncate_hardening"] is True
    assert c2a2["runtime_table_privileges"] == ["INSERT", "SELECT"]

    c2a3 = status["canonical_v120_persistence"]
    assert c2a3["id"] == "C2A3"
    assert c2a3["status"] == "EXTRACTED_AND_QUALIFIED"
    assert c2a3["replacement_pull_request"] == 121
    assert c2a3["strategy_dependency"] is False
    assert c2a3["broker_network_capability"] is False
    assert c2a3["order_write_capability"] is False


def test_c2a4_is_exact_qualified_strategy_free_excursion_persistence() -> None:
    status = load_status()
    c2a4 = status["canonical_v119_excursion_persistence"]

    assert c2a4["id"] == "C2A4"
    assert c2a4["status"] == "EXTRACTED_AND_QUALIFIED"
    assert c2a4["tracking_issue"] == 122
    assert c2a4["source_pull_request"] == 76
    assert c2a4["replacement_pull_request"] == 124
    assert c2a4["pre_merge_head_sha"] == (
        "00a0deffa954c70e44fb70483556746363228535"
    )
    assert c2a4["merge_sha"] == status["canonical_main"]["last_qualified_sha"]
    assert c2a4["pre_merge_exact_head_workflows_success"] == 10
    assert c2a4["post_merge_workflows_success"] == 11
    assert c2a4["post_merge_canonical_security_run_id"] == 34039454929
    assert c2a4["post_merge_release_provenance_run_id"] == 34039454985
    assert c2a4["postgresql_version_proven"] == "16.15"
    assert c2a4["runtime_migration_authority_allowed"] is False
    assert c2a4["runtime_truncate_or_ddl_allowed"] is False
    assert c2a4["strategy_dependency"] is False
    assert c2a4["broker_network_capability"] is False
    assert c2a4["market_data_dependency"] is False
    assert c2a4["order_write_capability"] is False
    assert c2a4["arm_halt_capability"] is False
    assert c2a4["demo_broker_proven"] is False
    assert c2a4["production_or_live_promotion_allowed"] is False
    assert c2a4["canonical_security_workflow_sha256"] == (
        "8ff9793125804197a828fbec6f56d574882147a62fb73c6201a5cdf6b56823dc"
    )


def test_current_consolidation_gate_is_c2b0_and_remains_fail_closed() -> None:
    status = load_status()
    consolidation = status["consolidation"]

    assert consolidation["status"] == "IN_PROGRESS_FAIL_CLOSED"
    assert consolidation["tracking_issue"] == 104
    assert consolidation["blind_merge_allowed"] is False
    assert consolidation["blind_close_allowed"] is False
    assert consolidation["branch_deletion_allowed"] is False
    assert consolidation["completed_gate"] == (
        "C2A4_STRATEGY_FREE_V119_ACTIVE_EXCURSION_CAS_PERSISTENCE"
    )
    assert consolidation["next_gate"] == (
        "C2B0_STRATEGY_FREE_V121_APPEND_ONLY_CONTROL_JOURNAL_PERSISTENCE"
    )
    assert consolidation["next_gate_issue"] == 125

    candidate = status["next_gate_candidate"]
    assert candidate["id"] == "C2B0"
    assert candidate["status"] == "IN_PROGRESS"
    assert candidate["tracking_issue"] == 125
    assert candidate["source_pull_request"] == 80
    assert SHA40.fullmatch(candidate["source_sha"])
    assert candidate["strategy_dependency_allowed"] is False
    assert candidate["broker_network_capability_allowed"] is False
    assert candidate["order_write_capability_allowed"] is False
    assert candidate["connected_preflight_dependency_allowed"] is False
    assert candidate["runtime_migration_authority_allowed"] is False
    assert candidate["production_or_live_promotion_allowed"] is False

    blockers = {item["id"]: item for item in status["current_blockers"]}
    assert "P1-C2A4-ACTIVE-EXCURSION-CAS-ISOLATION" not in blockers
    assert blockers["P1-C2B0-V121-CONTROL-JOURNAL-ISOLATION"]["tracking_issue"] == 125
    assert blockers["P1-V107-V109-APPEND-ONLY-TRUNCATE-HARDENING"]["tracking_issue"] == 109


def test_historical_operational_source_and_research_head_are_not_promoted() -> None:
    status = load_status()
    source = status["operational_source_boundary"]
    research = status["research_head"]

    assert source["pull_request"] == 93
    assert SHA40.fullmatch(source["sha"])
    assert source["status"] == "HISTORICAL_DECOMPOSITION_SOURCE_DEMO_UNPROVEN"
    assert source["wholesale_merge_allowed"] is False
    assert source["real_protected_demo_entry_proven"] is False
    assert source["complete_real_broker_evidence_chain_proven"] is False

    assert research["pull_request"] == 100
    assert SHA40.fullmatch(research["sha"])
    assert research["status"] == "RESEARCH_ONLY"
    assert research["derivatives_context_evidence"] == "INCOMPLETE"
    assert research["strategy_promotion_allowed"] is False


def test_governance_gap_remains_explicit() -> None:
    governance = load_status()["governance"]

    assert governance["main_branch_protection"] == "VERIFIED_DISABLED"
    assert governance["main_protected"] is False
    assert governance["required_status_checks_enforcement"] == "off"
    assert governance["independent_live_approver_assigned"] is False
    assert governance["tracking_issue"] == 103
