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
    assert status["observed_at"] == "2026-09-03"

    main = status["canonical_main"]
    assert main["last_qualified_sha"] == "fd04ad3403b7b840100006dbf1829273d9e5e4bb"
    assert main["engineering_baseline_status"] == "PASS"
    assert main["qualification"]["full_regression_result"] == (
        "1087 passed, 2 dedicated fleet-deployment skips"
    )
    assert main["qualification"]["postgres_full_regression_enabled"] is True
    assert main["qualification"]["release_attestation"] == "SLSA_AND_SBOM_SIGNED_PASS"

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


def test_c2a0_is_preserved_canonical_infrastructure_only() -> None:
    status = load_status()
    c2a0 = status["canonical_operational_foundation"]

    assert c2a0["id"] == "C2A0"
    assert c2a0["status"] == "EXTRACTED_AND_QUALIFIED"
    assert c2a0["source_preservation_pr"] == 110
    assert c2a0["replacement_pr"] == 113
    assert c2a0["merge_sha"] == "e110a4c02f5bf9b9937ff3fbf7e942859be9050d"
    assert c2a0["merge_sha"] != status["canonical_main"]["last_qualified_sha"]
    assert c2a0["frozen_migration_sha256"] == (
        "c37a2f54cb3dd42d6732b3354988d7f73cc1d240916ccbcdcec3874933f9d52e"
    )
    assert c2a0["research_ancestry_inherited"] is False
    assert c2a0["strategy_dependency"] is False
    assert c2a0["broker_network_capability"] is False
    assert c2a0["order_write_capability"] is False
    assert c2a0["demo_broker_proven"] is False
    assert c2a0["production_or_live_promotion_allowed"] is False


def test_c2a1_is_current_qualified_database_security_boundary() -> None:
    status = load_status()
    c2a1 = status["canonical_database_security"]

    assert c2a1["id"] == "C2A1"
    assert c2a1["status"] == "EXTRACTED_AND_QUALIFIED"
    assert c2a1["pull_request"] == 116
    assert c2a1["merge_sha"] == status["canonical_main"]["last_qualified_sha"]
    assert c2a1["runtime_role_separate_from_bootstrap"] is True
    assert c2a1["runtime_role_must_be_non_owner"] is True
    assert c2a1["runtime_database_create_allowed"] is False
    assert c2a1["runtime_public_schema_create_allowed"] is False
    assert c2a1["runtime_role_membership_allowed"] is False
    assert c2a1["lease_effective_privileges"] == ["DELETE", "INSERT", "SELECT"]
    assert c2a1["excursion_effective_privileges"] == [
        "DELETE",
        "INSERT",
        "SELECT",
        "UPDATE",
    ]
    assert c2a1["truncate_or_ddl_authority_allowed"] is False
    assert c2a1["postgresql_16_proven"] is True
    assert c2a1["strategy_dependency"] is False
    assert c2a1["broker_network_capability"] is False
    assert c2a1["order_write_capability"] is False
    assert c2a1["demo_broker_proven"] is False
    assert c2a1["production_or_live_promotion_allowed"] is False


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


def test_current_consolidation_gate_is_c2a2_and_remains_fail_closed() -> None:
    status = load_status()
    consolidation = status["consolidation"]

    assert consolidation["status"] == "IN_PROGRESS_FAIL_CLOSED"
    assert consolidation["tracking_issue"] == 104
    assert consolidation["blind_merge_allowed"] is False
    assert consolidation["blind_close_allowed"] is False
    assert consolidation["branch_deletion_allowed"] is False
    assert consolidation["completed_gate"] == "C2A1_V119_RUNTIME_ROLE_LEAST_PRIVILEGE"
    assert consolidation["next_gate"] == "C2A2_V120_APPEND_ONLY_AUDIT_HARDENING"
    assert consolidation["next_gate_issue"] == 117
    assert consolidation["next_gate_pr"] == 118
    assert consolidation["parent_database_security_issue"] == 107

    candidate = status["c2a2_candidate"]
    assert candidate["status"] == "IN_PROGRESS"
    assert candidate["tracking_issue"] == 117
    assert candidate["pull_request"] == 118
    assert candidate["historical_v120_001_modified"] is False
    assert candidate["runtime_table_privileges_target"] == ["INSERT", "SELECT"]
    assert candidate["typed_v120_persistence_promoted"] is False
    assert candidate["strategy_dependency_allowed"] is False
    assert candidate["broker_network_capability_allowed"] is False
    assert candidate["order_write_capability_allowed"] is False
    assert candidate["production_or_live_promotion_allowed"] is False

    blockers = {item["id"]: item for item in status["current_blockers"]}
    assert "P1-DATABASE-RUNTIME-ROLE" not in blockers
    hardening = blockers["P1-APPEND-ONLY-TRUNCATE-HARDENING"]
    assert hardening["status"] == "IN_PROGRESS"
    assert set(hardening["tracking_issues"]) == {109, 117}
    assert hardening["pull_request"] == 118


def test_governance_gap_remains_explicit() -> None:
    governance = load_status()["governance"]

    assert governance["main_branch_protection"] == "VERIFIED_DISABLED"
    assert governance["main_protected"] is False
    assert governance["required_status_checks_enforcement"] == "off"
    assert governance["independent_live_approver_assigned"] is False
    assert governance["tracking_issue"] == 103
