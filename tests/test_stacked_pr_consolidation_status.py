import json
import re
from pathlib import Path

STATUS_PATH = Path("STACKED_PR_CONSOLIDATION_STATUS.json")
OPERATIONAL_AUDIT_PATH = Path("OPERATIONAL_PRESERVATION_AUDIT_89_93.json")
PREREQUISITE_AUDIT_PATH = Path("OPERATIONAL_PREREQUISITE_AUDIT_75_88.json")
SHA40 = re.compile(r"^[0-9a-f]{40}$")


def load_status() -> dict:
    return json.loads(STATUS_PATH.read_text(encoding="utf-8"))


def load_operational_audit() -> dict:
    return json.loads(OPERATIONAL_AUDIT_PATH.read_text(encoding="utf-8"))


def load_prerequisite_audit() -> dict:
    return json.loads(PREREQUISITE_AUDIT_PATH.read_text(encoding="utf-8"))


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


def test_pr_89_93_audit_uses_current_exact_chain_and_forbids_wholesale_replay() -> None:
    audit = load_operational_audit()
    conclusion = audit["conclusion"]

    assert audit["schema_version"] == "operational-preservation-audit-89-93-v1"
    assert audit["scope"] == [89, 90, 91, 92, 93]
    assert conclusion["wholesale_merge_allowed"] is False
    assert conclusion["wholesale_cherry_pick_allowed"] is False
    assert conclusion["close_any_scoped_pr_allowed"] is False

    chain = audit["chain"]
    assert [row["pr"] for row in chain] == [89, 90, 91, 92, 93]
    assert all(row["head_verified_current"] is True for row in chain)
    assert all(SHA40.fullmatch(row["head_sha"]) for row in chain)
    assert all(SHA40.fullmatch(row["base_sha"]) for row in chain)

    by_pr = {row["pr"]: row for row in chain}
    for previous, current in ((89, 90), (90, 91), (91, 92), (92, 93)):
        assert by_pr[current]["base_sha"] == by_pr[previous]["head_sha"]

    assert by_pr[89]["changed_file_count"] == 17
    assert by_pr[90]["changed_file_count"] == 22
    assert by_pr[91]["changed_file_count"] == 19
    assert by_pr[92]["changed_file_count"] == 17
    assert by_pr[93]["changed_file_count"] == 23


def test_operational_canonicalization_is_capability_sliced_not_commit_order_replay() -> None:
    audit = load_operational_audit()
    slices = audit["capability_slice_order"]

    assert [entry["step"] for entry in slices] == [1, 2, 3, 4]
    assert slices[0]["name"] == "C2_PREREQUISITES_FROM_75_88"
    assert slices[1]["name"] == "C1_CURRENT_IDENTITY_AND_READINESS"
    assert slices[1]["source_prs"] == [91, 92, 93]
    assert slices[2]["name"] == "C3_PROTECTED_ONE_SHOT_ENTRY"
    assert slices[2]["source_prs"] == [89]
    assert slices[3]["name"] == "C4_EXACT_HEAD_EVIDENCE"
    assert slices[3]["source_prs"] == [90]

    by_pr = {row["pr"]: row for row in audit["chain"]}
    assert "AT_MOST_ONE_ENTRY_ATTEMPT" in by_pr[89]["safety_invariants"]
    assert "NO_BLIND_RESUBMIT" in by_pr[89]["safety_invariants"]
    assert "NO_MAINNET_WRITE_PATH" in by_pr[89]["safety_invariants"]
    assert "NO_ORDER_CAPABLE_CLIENT_BEFORE_IDENTITY_PASS" in by_pr[91]["safety_invariants"]
    assert "ONE_IMMUTABLE_LOGICAL_DATABASE_UUID" in by_pr[93]["safety_invariants"]


def test_pr_75_88_prerequisite_audit_is_fail_closed_and_preserves_final_capabilities() -> None:
    audit = load_prerequisite_audit()
    decision = audit["decision"]

    assert audit["schema_version"] == "operational-prerequisite-audit-75-88-v1"
    assert audit["scope"] == list(range(75, 89))
    assert decision["wholesale_merge_allowed"] is False
    assert decision["wholesale_cherry_pick_allowed"] is False
    assert decision["close_any_scoped_pr_allowed"] is False
    assert decision["intermediate_version_replay_required"] is False
    assert decision["canonicalize_capabilities_from_current_final_forms"] is True

    chain = audit["chain"]
    assert [row["pr"] for row in chain] == list(range(75, 89))
    assert all(SHA40.fullmatch(row["head_sha"]) for row in chain)
    assert all(SHA40.fullmatch(row["base_sha"]) for row in chain)

    by_pr = {row["pr"]: row for row in chain}
    for previous, current in zip(range(75, 87), range(76, 88), strict=True):
        assert by_pr[current]["base_sha"] == by_pr[previous]["head_sha"]

    assert by_pr[88]["base_sha"] != by_pr[87]["head_sha"]
    assert "diverge" in by_pr[88]["ancestry_note"].lower()
    assert by_pr[88]["preservation"] == "REQUIRED_C2_RECOVERY"


def test_prerequisite_audit_preserves_v119_v124_lineage_without_replaying_old_wrappers() -> None:
    audit = load_prerequisite_audit()
    migrations = audit["migration_lineage_to_preserve"]

    assert [row["version"] for row in migrations] == [
        "v119",
        "v120",
        "v121",
        "v122",
        "v123",
        "v124",
    ]
    assert [row["source_pr"] for row in migrations] == [76, 77, 80, 84, 88, 93]
    assert migrations[2]["hardened_by_pr"] == 88

    decisions = {row["pr"]: row for row in audit["intermediate_implementation_decisions"]}
    assert decisions[79]["replacement"] == "PR #93 v119-v124 bootstrap"
    assert decisions[83]["replacement"] == "PR #93 v124 activation readiness wrapper/current source artifacts"
    assert decisions[88]["decision"] == "DO_NOT_ASSUME_PR87_CURRENT_HEAD_IS_ANCESTOR"


def test_prerequisite_capability_slices_keep_readiness_control_risk_and_entry_lineage_separate() -> None:
    audit = load_prerequisite_audit()
    slices = {row["id"]: row for row in audit["required_capability_slices"]}

    assert slices["C2A"]["source_prs"] == [76, 77]
    assert slices["C2B"]["source_prs"] == [80, 84, 85, 86]
    assert slices["C2C"]["source_prs"] == [87, 88]
    assert slices["C1A"]["source_prs"] == [78, 81, 82]
    assert slices["C1B"]["source_prs"] == [79, 83, 93]
    assert slices["C1B"]["canonical_source"] == "PR_93_FINAL_V124_FORMS"
    assert slices["C3A"]["source_prs"] == [75]

    assert audit["next_canonicalization_gate"] == (
        "BUILD_C2A_FOUNDATION_DIFF_FROM_CURRENT_MAIN_WITHOUT_RESEARCH_ANCESTRY"
    )
