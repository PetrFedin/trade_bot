import json
import re
from pathlib import Path

STATUS_PATH = Path("CURRENT_SYSTEM_STATUS.json")
SHA40 = re.compile(r"^[0-9a-f]{40}$")


def load_status() -> dict:
    return json.loads(STATUS_PATH.read_text(encoding="utf-8"))


def test_current_system_status_is_fail_closed_and_does_not_claim_profitability() -> None:
    status = load_status()

    assert status["schema_version"] == "current-system-status-v1"
    assert status["canonical_main"]["engineering_baseline_status"] == "PASS"
    assert SHA40.fullmatch(status["canonical_main"]["last_qualified_sha"])

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


def test_current_system_status_separates_main_operational_and_research_heads() -> None:
    status = load_status()

    operational = status["operational_candidate"]
    research = status["research_head"]

    assert operational["pull_request"] == 93
    assert SHA40.fullmatch(operational["sha"])
    assert operational["status"] == "CODE_QUALIFIED_DEMO_UNPROVEN"
    assert operational["real_protected_demo_entry_proven"] is False
    assert operational["complete_real_broker_evidence_chain_proven"] is False

    assert research["pull_request"] == 100
    assert SHA40.fullmatch(research["sha"])
    assert research["status"] == "RESEARCH_ONLY"
    assert research["derivatives_context_evidence"] == "INCOMPLETE"
    assert research["strategy_promotion_allowed"] is False

    assert operational["sha"] != research["sha"]
    assert operational["sha"] != status["canonical_main"]["last_qualified_sha"]


def test_current_system_status_keeps_verified_governance_gap_explicit() -> None:
    status = load_status()
    governance = status["governance"]

    assert governance["main_branch_protection"] == "VERIFIED_DISABLED"
    assert governance["main_protected"] is False
    assert governance["required_status_checks_enforcement"] == "off"
    assert governance["independent_live_approver_assigned"] is False
    assert governance["tracking_issue"] == 103

    blockers = {item["id"]: item for item in status["current_blockers"]}
    assert blockers["P0-GOVERNANCE"]["status"] == "BLOCKED"
    assert blockers["STRATEGY-EDGE"]["status"] == "FAIL"
    assert blockers["BYBIT-DEMO-ENTRY"]["status"] == "NOT_STARTED"
    assert blockers["EXACT-HEAD-OPERATIONAL-EVIDENCE"]["status"] == "NOT_STARTED"
