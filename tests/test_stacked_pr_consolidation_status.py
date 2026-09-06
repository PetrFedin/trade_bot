import json
import re
from pathlib import Path

STATUS_PATH = Path("STACKED_PR_CONSOLIDATION_STATUS.json")
OPERATIONAL_AUDIT_PATH = Path("OPERATIONAL_PRESERVATION_AUDIT_89_93.json")
PREREQUISITE_AUDIT_PATH = Path("OPERATIONAL_PREREQUISITE_AUDIT_75_88.json")
SHA40 = re.compile(r"^[0-9a-f]{40}$")


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_current_consolidation_status_is_fail_closed() -> None:
    status = load(STATUS_PATH)
    policy = status["policy"]

    assert status["schema_version"] == "stacked-pr-consolidation-status-v2"
    assert status["observed_at"] == "2026-09-06"
    assert policy["blind_merge_allowed"] is False
    assert policy["blind_close_allowed"] is False
    assert policy["delete_branch_allowed"] is False
    assert policy["production_activation_allowed"] is False
    assert policy["strategy_promotion_allowed"] is False
    assert policy["research_ancestry_allowed_in_operational_extraction"] is False
    assert "POST_MERGE_MAIN_QUALIFICATION_PASS" in policy["required_before_operational_merge"]


def test_current_boundaries_keep_release_source_and_research_separate() -> None:
    status = load(STATUS_PATH)
    boundaries = status["current_boundaries"]

    main = boundaries["canonical_main"]
    source = boundaries["operational_source_boundary"]
    research = boundaries["research_head"]

    assert main["sha"] == "ff684ab85b92151c215c7d5cc85bfc527fecb5eb"
    assert SHA40.fullmatch(main["sha"])
    assert main["broker_proven"] is False
    assert main["demo_entry_proven"] is False
    assert main["strategy_profitability_proven"] is False
    assert source["pull_request"] == 93
    assert source["wholesale_merge_allowed"] is False
    assert research["pull_request"] == 100
    assert research["status"] == "RESEARCH_ONLY"
    assert research["merge_into_operational_core_allowed"] is False
    assert len({main["sha"], source["sha"], research["sha"]}) == 3


def test_c2a0_through_c2b0_are_explicitly_completed() -> None:
    status = load(STATUS_PATH)
    completed = status["completed_canonical_extractions"]
    assert [row["id"] for row in completed] == [
        "C2A0",
        "C2A1",
        "C2A2",
        "C2A3",
        "C2A4",
        "C2B0",
    ]
    assert all(row["status"] == "EXTRACTED_AND_QUALIFIED" for row in completed)

    c2b0 = {row["id"]: row for row in completed}["C2B0"]
    assert c2b0["replacement_pr"] == 127
    assert c2b0["tracking_issue"] == 125
    assert c2b0["pre_merge_head_sha"] == (
        "e9df55204d5127e585166fd364612ab940d114a1"
    )
    assert c2b0["merge_sha"] == status["current_boundaries"]["canonical_main"]["sha"]
    assert c2b0["frozen_v121_001_git_blob"] == (
        "cae1dd432050f235b94d230b2e46c862d38b58c6"
    )
    assert c2b0["frozen_v121_001_sha256"] == (
        "a03a738d7036c59338ef2ebe085a28a266fd0b440d97ca8e3fae59379efe8e21"
    )
    assert c2b0["reader_table_privileges"] == ["SELECT"]
    assert c2b0["writer_table_privileges"] == ["INSERT", "SELECT"]
    assert c2b0["writer_sequence_privileges"] == ["USAGE"]
    assert c2b0["strategy_dependency"] is False
    assert c2b0["broker_network_capability"] is False
    assert c2b0["connected_preflight_acquisition_capability"] is False
    assert c2b0["order_write_capability"] is False
    assert c2b0["runtime_migration_authority"] is False
    assert c2b0["qualification"]["pre_merge_workflows_success"] == 10
    assert c2b0["qualification"]["post_merge_workflows_success"] == 11
    assert c2b0["qualification"]["canonical_security_regression_run_id"] == 34056249717
    assert c2b0["qualification"]["release_provenance_run_id"] == 34056249725
    assert c2b0["qualification"]["canonical_deployment_regression_run_id"] == 34056249729


def test_c2b1_is_current_gate_and_keeps_connected_trading_out() -> None:
    status = load(STATUS_PATH)
    gate = status["current_gate"]

    assert gate["id"] == "C2B1"
    assert gate["issue"] == 130
    assert gate["status"] == "AUDIT_IN_PROGRESS"
    assert gate["source_pr"] == 84
    assert gate["source_sha"] == "bdcdf7189b56b494e14ac746ad9b867c108dcd30"
    assert gate["persistence_refinement_pr"] == 86
    assert gate["persistence_refinement_sha"] == (
        "700e59e0b67329ea7df00bbd78aebd6ddfdba334"
    )
    assert gate["historical_migration_blob"] == (
        "25f05f93cf11165416e999321ac5d4a422b9a1a3"
    )
    assert gate["strategy_dependency_allowed"] is False
    assert gate["accounting_builder_dependency_allowed"] is False
    assert gate["broker_network_capability_allowed"] is False
    assert gate["connected_session_start_allowed"] is False
    assert gate["runtime_order_capability_allowed"] is False
    assert gate["runtime_migration_authority_allowed"] is False

    c2 = next(row for row in status["remaining_work_packages"] if row["id"] == "C2")
    assert c2["completed"] == ["C2A0", "C2A1", "C2A2", "C2A3", "C2A4", "C2B0"]
    assert c2["next"] == "C2B1"

    blocked = {row["id"]: row for row in status["blocked_follow_on"]}
    assert blocked["CANONICAL_V107_V109_APPEND_ONLY_TRUNCATE_HARDENING"]["tracking_issue"] == 109
    assert blocked["CONNECTED_SESSION_START"]["status"].startswith("BLOCKED")
    assert blocked["TERMINAL_RISK_CHECKPOINT_HANDOFF"]["status"].startswith("BLOCKED")
    assert blocked["PROTECTED_DEMO_ENTRY"]["status"].startswith("BLOCKED")


def test_historical_operational_audits_remain_fail_closed_evidence() -> None:
    audit = load(OPERATIONAL_AUDIT_PATH)
    prereq = load(PREREQUISITE_AUDIT_PATH)

    assert audit["schema_version"] == "operational-preservation-audit-89-93-v1"
    assert audit["conclusion"]["wholesale_merge_allowed"] is False
    assert audit["conclusion"]["wholesale_cherry_pick_allowed"] is False

    assert prereq["schema_version"] == "operational-prerequisite-audit-75-88-v1"
    assert prereq["decision"]["wholesale_merge_allowed"] is False
    assert prereq["decision"]["wholesale_cherry_pick_allowed"] is False
    assert prereq["decision"]["canonicalize_capabilities_from_current_final_forms"] is True
    assert [row["version"] for row in prereq["migration_lineage_to_preserve"]] == [
        "v119",
        "v120",
        "v121",
        "v122",
        "v123",
        "v124",
    ]


def test_historical_inventory_is_archived_not_deleted() -> None:
    status = load(STATUS_PATH)
    historical = status["historical_inventory"]

    assert historical["machine_snapshot"] == (
        "docs/archive/2026-09-02/STACKED_PR_CONSOLIDATION_STATUS_V1.json"
    )
    assert historical["human_snapshot"] == (
        "docs/archive/2026-09-02/STACKED_PR_CONSOLIDATION_MAP_V1.md"
    )
