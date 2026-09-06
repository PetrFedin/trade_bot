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


def test_current_boundaries_keep_main_operational_source_and_research_separate() -> None:
    status = load(STATUS_PATH)
    boundaries = status["current_boundaries"]

    main = boundaries["canonical_main"]
    source = boundaries["operational_source_boundary"]
    research = boundaries["research_head"]

    assert main["sha"] == "043fc3003b055dc1e953854798627048d3f26960"
    assert SHA40.fullmatch(main["sha"])
    assert main["broker_proven"] is False
    assert main["demo_entry_proven"] is False
    assert main["strategy_profitability_proven"] is False
    assert source["pull_request"] == 93
    assert SHA40.fullmatch(source["sha"])
    assert source["wholesale_merge_allowed"] is False
    assert research["pull_request"] == 100
    assert SHA40.fullmatch(research["sha"])
    assert research["status"] == "RESEARCH_ONLY"
    assert research["merge_into_operational_core_allowed"] is False
    assert len({main["sha"], source["sha"], research["sha"]}) == 3


def test_c2a0_through_c2a4_are_explicitly_completed() -> None:
    status = load(STATUS_PATH)
    completed = status["completed_canonical_extractions"]
    assert [row["id"] for row in completed] == ["C2A0", "C2A1", "C2A2", "C2A3", "C2A4"]
    assert all(row["status"] == "EXTRACTED_AND_QUALIFIED" for row in completed)

    by_id = {row["id"]: row for row in completed}
    c2a0 = by_id["C2A0"]
    assert c2a0["source_preservation_pr"] == 110
    assert c2a0["replacement_pr"] == 113
    assert c2a0["merge_sha"] == "e110a4c02f5bf9b9937ff3fbf7e942859be9050d"
    assert c2a0["preservation_method"] == "EXACT_FIVE_GIT_BLOBS"
    assert c2a0["research_ancestry_inherited"] is False
    assert c2a0["strategy_dependency"] is False
    assert c2a0["network_or_order_capability"] is False
    assert c2a0["demo_proven"] is False

    c2a1 = by_id["C2A1"]
    assert c2a1["replacement_pr"] == 116
    assert c2a1["runtime_role_non_owner"] is True
    assert c2a1["runtime_ddl_allowed"] is False
    assert c2a1["network_or_order_capability"] is False

    c2a2 = by_id["C2A2"]
    assert c2a2["replacement_pr"] == 118
    assert c2a2["frozen_v120_001_git_blob"] == (
        "b337ef19dc7da4a3fcbc0a11a8d6d7d85dff3b00"
    )
    assert c2a2["runtime_table_privileges"] == ["INSERT", "SELECT"]

    c2a3 = by_id["C2A3"]
    assert c2a3["replacement_pr"] == 121
    assert c2a3["merge_sha"] == "090f34b11a877ce24f8a15a74b296e287aae3918"
    assert c2a3["merge_sha"] != status["current_boundaries"]["canonical_main"]["sha"]
    assert c2a3["strategy_dependency"] is False
    assert c2a3["broker_network_capability"] is False
    assert c2a3["market_data_dependency"] is False
    assert c2a3["order_write_capability"] is False
    assert c2a3["qualification"]["post_merge_workflows_success"] == 11
    assert c2a3["qualification"]["full_postgres_regression"] == (
        "1112 passed, 2 dedicated fleet-deployment skips"
    )

    c2a4 = by_id["C2A4"]
    assert c2a4["source_pr"] == 76
    assert c2a4["replacement_pr"] == 124
    assert c2a4["pre_merge_head_sha"] == "00a0deffa954c70e44fb70483556746363228535"
    assert c2a4["merge_sha"] == status["current_boundaries"]["canonical_main"]["sha"]
    assert c2a4["strategy_dependency"] is False
    assert c2a4["broker_network_capability"] is False
    assert c2a4["market_data_dependency"] is False
    assert c2a4["order_write_capability"] is False
    assert c2a4["runtime_migration_authority_allowed"] is False
    assert c2a4["qualification"]["post_merge_workflows_success"] == 11
    assert c2a4["qualification"]["focused_security_regression"] == "282 passed"
    assert c2a4["qualification"]["full_postgres_regression"] == (
        "1124 passed, 2 dedicated fleet-deployment skips"
    )

    assert status["supersession_records"] == [
        {
            "pull_request": 110,
            "state": "CLOSED_NOT_MERGED",
            "superseded_by": 113,
            "reason": (
                "The same five audited Git blobs were extracted onto repaired canonical main "
                "without inheriting the old branch ancestry."
            ),
        }
    ]


def test_c2b0_is_current_gate_and_keeps_trading_capability_absent() -> None:
    status = load(STATUS_PATH)
    gate = status["current_gate"]

    assert gate["id"] == "C2B0"
    assert gate["issue"] == 128
    assert gate["status"] == "AUDIT_IN_PROGRESS"
    assert gate["source_pr"] == 80
    assert SHA40.fullmatch(gate["source_sha"])
    assert gate["hardening_source_pr"] == 88
    assert SHA40.fullmatch(gate["hardening_source_sha"])
    assert gate["frozen_v121_001_git_blob"] == (
        "cae1dd432050f235b94d230b2e46c862d38b58c6"
    )
    assert gate["runtime_order_capability_allowed"] is False
    assert gate["broker_network_capability_allowed"] is False
    assert gate["connected_preflight_dependency_allowed"] is False
    assert gate["strategy_dependency_allowed"] is False
    assert gate["automatic_arm_allowed"] is False
    assert gate["runtime_migration_authority_allowed"] is False

    blocked = {row["id"]: row for row in status["blocked_follow_on"]}
    assert blocked["CANONICAL_V107_V109_APPEND_ONLY_TRUNCATE_HARDENING"]["tracking_issue"] == 109
    assert blocked["PROTECTED_DEMO_ENTRY"]["status"].startswith("BLOCKED")
    assert blocked["EXACT_HEAD_OPERATIONAL_EVIDENCE"]["status"].startswith("BLOCKED")

    c2 = next(row for row in status["remaining_work_packages"] if row["id"] == "C2")
    assert c2["completed"] == ["C2A0", "C2A1", "C2A2", "C2A3", "C2A4"]
    assert c2["next"] == "C2B0"


def test_historical_operational_audits_remain_fail_closed_evidence() -> None:
    audit = load(OPERATIONAL_AUDIT_PATH)
    prereq = load(PREREQUISITE_AUDIT_PATH)

    assert audit["schema_version"] == "operational-preservation-audit-89-93-v1"
    assert audit["conclusion"]["wholesale_merge_allowed"] is False
    assert audit["conclusion"]["wholesale_cherry_pick_allowed"] is False
    assert audit["conclusion"]["close_any_scoped_pr_allowed"] is False

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

    c2b = next(row for row in prereq["required_capability_slices"] if row["id"] == "C2B")
    assert c2b["source_prs"] == [80, 84, 85, 86]
    assert "v121 append-only ARM/HALT journal and cryptographic event identity" in c2b[
        "required_artifacts"
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
