import json
import re
from pathlib import Path

STATUS_PATH = Path("STACKED_PR_CONSOLIDATION_STATUS.json")
SHA40 = re.compile(r"^[0-9a-f]{40}$")


def load_status() -> dict:
    return json.loads(STATUS_PATH.read_text(encoding="utf-8"))


def test_consolidation_policy_is_fail_closed() -> None:
    status = load_status()
    policy = status["policy"]

    assert status["schema_version"] == "stacked-pr-consolidation-status-v1"
    assert policy["blind_merge_allowed"] is False
    assert policy["blind_close_allowed"] is False
    assert policy["delete_branch_allowed"] is False
    assert policy["production_activation_allowed"] is False
    assert policy["strategy_promotion_allowed"] is False

    required = set(policy["required_before_merge_or_close"])
    assert {
        "CURRENT_HEAD_REVERIFIED",
        "CHANGED_FILES_INVENTORIED",
        "UNIQUE_RUNTIME_CODE_PRESERVED_OR_SUPERSEDED",
        "UNIQUE_MIGRATIONS_PRESERVED_OR_SUPERSEDED",
        "IMMUTABLE_EVIDENCE_AND_DOCS_PRESERVED_OR_ARCHIVED",
        "QUALIFICATION_PLAN_IDENTIFIED",
    }.issubset(required)


def test_operational_and_research_boundaries_are_not_merge_promotions() -> None:
    status = load_status()
    boundaries = status["current_boundaries"]

    main = boundaries["canonical_main"]
    operational = boundaries["operational_boundary_candidate"]
    research = boundaries["research_head"]

    assert SHA40.fullmatch(main["sha"])
    assert operational["pull_request"] == 93
    assert SHA40.fullmatch(operational["sha"])
    assert operational["status"] == "CODE_QUALIFIED_DEMO_UNPROVEN"
    assert operational["merge_decision"] == "PROHIBITED_PENDING_DECOMPOSITION"

    assert research["pull_request"] == 100
    assert SHA40.fullmatch(research["sha"])
    assert research["status"] == "RESEARCH_ONLY"
    assert research["merge_decision"] == "PROHIBITED_PENDING_RESEARCH_ISOLATION"

    assert len({main["sha"], operational["sha"], research["sha"]}) == 3


def test_every_open_stack_pr_has_exactly_one_fail_closed_classification() -> None:
    status = load_status()
    rows = status["open_stack_prs"]
    pr_numbers = [row["pr"] for row in rows]

    assert len(pr_numbers) == len(set(pr_numbers))
    assert pr_numbers == sorted(pr_numbers)
    assert pr_numbers[0] == 41
    assert pr_numbers[-1] == 100

    allowed_actions = {
        "PRESERVE_PENDING_FILE_AUDIT",
        "ISOLATE_RESEARCH",
        "DECOUPLE_BEFORE_CANONICALIZATION",
        "EXTRACT_AFTER_FILE_AUDIT",
        "DECOMPOSE_AND_PRESERVE",
        "PRESERVE_EVIDENCE_DO_NOT_PROMOTE",
    }
    assert all(row["action"] in allowed_actions for row in rows)
    assert not any(row["action"] in {"MERGE", "CLOSE", "DELETE_BRANCH"} for row in rows)


def test_research_ranges_cannot_be_silently_promoted_into_operational_core() -> None:
    status = load_status()
    ranges = {entry["id"]: entry for entry in status["ranges"]}

    for range_id in ("R4", "R6", "R9"):
        entry = ranges[range_id]
        assert entry["target"] == "ISOLATED_RESEARCH_LINEAGE"
        assert entry["action"] in {
            "DO_NOT_MERGE_INTO_OPERATIONAL_CORE_AS_A_STACK",
            "PRESERVE_EVIDENCE_DO_NOT_PROMOTE",
        }

    assert ranges["R5"]["action"] == "DECOUPLE_BEFORE_CANONICALIZATION"
    assert ranges["R8"]["action"] == "DECOMPOSE_AND_PRESERVE"
