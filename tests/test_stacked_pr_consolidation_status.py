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
    assert status["observed_at"] == "2026-09-03"
    assert policy["blind_merge_allowed"] is False
    assert policy["blind_close_allowed"] is False
    assert policy["delete_branch_allowed"] is False
    assert policy["production_activation_allowed"] is False
    assert policy["strategy_promotion_allowed"] is False
    assert policy["research_ancestry_allowed_in_operational_extraction"] is False


def test_current_boundaries_keep_main_operational_source_and_research_separate() -> None:
    status = load(STATUS_PATH)
    boundaries = status["current_boundaries"]

    main = boundaries["canonical_main"]
    source = boundaries["operational_source_boundary"]
    research = boundaries["research_head"]

    assert main["sha"] == "e110a4c02f5bf9b9937ff3fbf7e942859be9050d"
    assert SHA40.fullmatch(main["sha"])
    assert source["pull_request"] == 93
    assert SHA40.fullmatch(source["sha"])
    assert source["wholesale_merge_allowed"] is False
    assert research["pull_request"] == 100
    assert SHA40.fullmatch(research["sha"])
    assert research["status"] == "RESEARCH_ONLY"
    assert research["merge_into_operational_core_allowed"] is False
    assert len({main["sha"], source["sha"], research["sha"]}) == 3


def test_c2a0_preservation_and_supersession_are_explicit() -> None:
    status = load(STATUS_PATH)
    completed = status["completed_canonical_extractions"]
    assert len(completed) == 1

    c2a0 = completed[0]
    assert c2a0["id"] == "C2A0"
    assert c2a0["source_preservation_pr"] == 110
    assert c2a0["replacement_pr"] == 113
    assert c2a0["merge_sha"] == status["current_boundaries"]["canonical_main"]["sha"]
    assert c2a0["status"] == "EXTRACTED_AND_QUALIFIED"
    assert c2a0["preservation_method"] == "EXACT_FIVE_GIT_BLOBS"
    assert c2a0["research_ancestry_inherited"] is False
    assert c2a0["strategy_dependency"] is False
    assert c2a0["network_or_order_capability"] is False
    assert c2a0["demo_proven"] is False
    assert c2a0["qualification"]["full_postgres_regression"] == (
        "1078 passed, 2 dedicated fleet-deployment skips"
    )

    superseded = status["supersession_records"]
    assert superseded == [
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


def test_c2a1_is_next_and_v120_remains_blocked() -> None:
    status = load(STATUS_PATH)
    gate = status["current_gate"]

    assert gate["id"] == "C2A1"
    assert gate["issue"] == 114
    assert gate["parent_security_issue"] == 107
    assert gate["status"] == "IN_PROGRESS"
    assert gate["required_before_v120_extraction"] is True
    assert gate["runtime_order_capability_allowed"] is False
    assert gate["broker_network_capability_allowed"] is False

    blocked = {row["id"]: row for row in status["blocked_follow_on"]}
    assert blocked["V120_DURABLE_AUDIT_STORES"]["status"] == "BLOCKED"
    assert blocked["CANONICAL_V107_V109_APPEND_ONLY_TRUNCATE_HARDENING"]["tracking_issue"] == 109


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


def test_historical_inventory_is_archived_not_deleted() -> None:
    status = load(STATUS_PATH)
    historical = status["historical_inventory"]

    assert historical["machine_snapshot"] == (
        "docs/archive/2026-09-02/STACKED_PR_CONSOLIDATION_STATUS_V1.json"
    )
    assert historical["human_snapshot"] == (
        "docs/archive/2026-09-02/STACKED_PR_CONSOLIDATION_MAP_V1.md"
    )
