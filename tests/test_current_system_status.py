import json
import re
from pathlib import Path

STATUS_PATH = Path("CURRENT_SYSTEM_STATUS.json")
SHA40 = re.compile(r"^[0-9a-f]{40}$")


def load_status() -> dict:
    return json.loads(STATUS_PATH.read_text(encoding="utf-8"))


def test_current_system_status_is_c2b0_qualified_and_fail_closed() -> None:
    status = load_status()

    assert status["schema_version"] == "current-system-status-v2"
    assert status["observed_at"] == "2026-09-06"

    main = status["canonical_main"]
    assert main["last_qualified_sha"] == (
        "ff684ab85b92151c215c7d5cc85bfc527fecb5eb"
    )
    assert main["engineering_baseline_status"] == "PASS"
    qualification = main["qualification"]
    assert qualification["post_merge_workflows_completed"] == 11
    assert qualification["post_merge_workflows_success"] == 11
    assert qualification["canonical_security_regression_run_id"] == 34056249717
    assert qualification["release_provenance_run_id"] == 34056249725
    assert qualification["canonical_deployment_regression_run_id"] == 34056249729
    assert qualification["postgres_full_regression_enabled"] is True
    assert qualification["postgres_version"] == "16"
    assert qualification["release_attestation"] == "SLSA_AND_SBOM_SIGNED_PASS"

    strategy = status["strategy"]
    assert strategy["status"] == "PROFITABILITY_NOT_PROVEN"
    assert strategy["promotion_allowed"] is False
    assert float(strategy["latest_frozen_bybit_price_only_replay"]["net_pnl_usdt"]) < 0

    live = status["live"]
    assert live["status"] == "FAIL_CLOSED"
    assert live["external_order_routing_allowed"] is False
    assert live["live_trading_allowed"] is False
    assert live["mainnet_entry_allowed"] is False
    assert live["production_release_allowed"] is False


def test_c2b0_is_exact_qualified_strategy_free_control_journal() -> None:
    status = load_status()
    c2b0 = status["canonical_v121_control_journal"]

    assert c2b0["id"] == "C2B0"
    assert c2b0["status"] == "EXTRACTED_AND_QUALIFIED"
    assert c2b0["tracking_issue"] == 125
    assert c2b0["replacement_pull_request"] == 127
    assert c2b0["pre_merge_head_sha"] == (
        "e9df55204d5127e585166fd364612ab940d114a1"
    )
    assert c2b0["merge_sha"] == status["canonical_main"]["last_qualified_sha"]
    assert c2b0["frozen_v121_001_git_blob"] == (
        "cae1dd432050f235b94d230b2e46c862d38b58c6"
    )
    assert c2b0["frozen_v121_001_sha256"] == (
        "a03a738d7036c59338ef2ebe085a28a266fd0b440d97ca8e3fae59379efe8e21"
    )
    assert c2b0["canonical_security_workflow_sha256"] == (
        "5977182e1ae6fab8da08b5b1addbf7bb23a02af0477332e32f92349cd2ea61dd"
    )
    assert c2b0["reader_table_privileges"] == ["SELECT"]
    assert c2b0["writer_table_privileges"] == ["INSERT", "SELECT"]
    assert c2b0["writer_sequence_privileges"] == ["USAGE"]
    assert c2b0["exact_public_before_trigger_binding_required"] is True
    assert c2b0["historical_arm_preflight_safety_semantics_preserved"] is True
    assert c2b0["pre_merge_exact_head_workflows_success"] == 10
    assert c2b0["post_merge_workflows_success"] == 11
    assert c2b0["post_merge_canonical_security_run_id"] == 34056249717
    assert c2b0["post_merge_release_provenance_run_id"] == 34056249725
    assert c2b0["post_merge_canonical_deployment_run_id"] == 34056249729
    assert c2b0["runtime_migration_authority_allowed"] is False
    assert c2b0["strategy_dependency"] is False
    assert c2b0["broker_network_capability"] is False
    assert c2b0["connected_preflight_acquisition_capability"] is False
    assert c2b0["order_write_capability"] is False
    assert c2b0["automatic_arm_allowed"] is False
    assert c2b0["demo_broker_proven"] is False
    assert c2b0["production_or_live_promotion_allowed"] is False


def test_current_consolidation_gate_is_c2b1_and_remains_non_trading() -> None:
    status = load_status()
    consolidation = status["consolidation"]

    assert consolidation["status"] == "IN_PROGRESS_FAIL_CLOSED"
    assert consolidation["tracking_issue"] == 104
    assert consolidation["completed_gate"] == (
        "C2B0_STRATEGY_FREE_V121_APPEND_ONLY_CONTROL_JOURNAL_PERSISTENCE"
    )
    assert consolidation["next_gate"] == (
        "C2B1_STRATEGY_FREE_V122_RESTART_SAFE_SESSION_RISK_PERSISTENCE"
    )
    assert consolidation["next_gate_issue"] == 130

    candidate = status["next_gate_candidate"]
    assert candidate["id"] == "C2B1"
    assert candidate["status"] == "AUDIT_IN_PROGRESS"
    assert candidate["tracking_issue"] == 130
    assert candidate["source_pull_request"] == 84
    assert SHA40.fullmatch(candidate["source_sha"])
    assert candidate["persistence_refinement_pull_request"] == 86
    assert SHA40.fullmatch(candidate["persistence_refinement_sha"])
    assert candidate["frozen_v122_001_git_blob"] == (
        "25f05f93cf11165416e999321ac5d4a422b9a1a3"
    )
    assert candidate["frozen_v122_001_sha256"] == "PENDING_RUNNER_MEASUREMENT"
    assert candidate["strategy_dependency_allowed"] is False
    assert candidate["accounting_builder_dependency_allowed"] is False
    assert candidate["broker_network_capability_allowed"] is False
    assert candidate["order_write_capability_allowed"] is False
    assert candidate["connected_session_start_allowed"] is False
    assert candidate["runtime_migration_authority_allowed"] is False

    blockers = {item["id"]: item for item in status["current_blockers"]}
    assert "P1-C2B0-V121-CONTROL-JOURNAL-ISOLATION" not in blockers
    assert blockers["P1-C2B1-V122-SESSION-RISK-PERSISTENCE"]["tracking_issue"] == 130
    assert blockers["P1-V107-V109-APPEND-ONLY-TRUNCATE-HARDENING"]["tracking_issue"] == 109


def test_historical_operational_source_research_and_governance_are_not_promoted() -> None:
    status = load_status()
    source = status["operational_source_boundary"]
    research = status["research_head"]
    governance = status["governance"]

    assert source["pull_request"] == 93
    assert SHA40.fullmatch(source["sha"])
    assert source["wholesale_merge_allowed"] is False
    assert source["real_protected_demo_entry_proven"] is False
    assert source["complete_real_broker_evidence_chain_proven"] is False

    assert research["pull_request"] == 100
    assert research["status"] == "RESEARCH_ONLY"
    assert research["strategy_promotion_allowed"] is False

    assert governance["main_branch_protection"] == "VERIFIED_DISABLED"
    assert governance["main_protected"] is False
    assert governance["required_status_checks_enforcement"] == "off"
    assert governance["independent_live_approver_assigned"] is False
    assert governance["tracking_issue"] == 103
