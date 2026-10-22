"""Regression and adversarial tests for offline evidence lineage and blocker propagation."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from tools import research_evidence_lineage as lineage
from tools.research_evidence_lineage import EvidenceLineageError

ROOT = lineage.ROOT
STATUS = json.loads(lineage.STATUS_PATH.read_text(encoding="utf-8"))
REGISTER = json.loads(lineage.REGISTER_PATH.read_text(encoding="utf-8"))
POINTER = json.loads(lineage.EVIDENCE_PATH.read_text(encoding="utf-8"))
DIGESTS = {
    "status": hashlib.sha256(lineage.STATUS_PATH.read_bytes()).hexdigest(),
    "capabilities": hashlib.sha256(lineage.REGISTER_PATH.read_bytes()).hexdigest(),
    "strategy_pointer": hashlib.sha256(lineage.EVIDENCE_PATH.read_bytes()).hexdigest(),
}


def _dossier(status=None, register=None, pointer=None, digests=None):
    return lineage.create_dossier(
        STATUS if status is None else status,
        REGISTER if register is None else register,
        POINTER if pointer is None else pointer,
        root=ROOT,
        source_digests=DIGESTS if digests is None else digests,
    )


def test_original_frozen_sources_agree_without_claiming_broker_proof():
    result = _dossier()
    assert result["decision"] == "RESEARCH_HOLD"
    assert result["scope"] == "OFFLINE_REPOSITORY_SNAPSHOT_NOT_RUNTIME_OR_PROVIDER_PROOF"
    assert result["portfolio"]["net_pnl_usdt"] == "-176.67"
    assert result["portfolio"]["portfolio_trades"] == 102
    assert result["frozen_pointer"]["value_agreement"] is True
    assert result["frozen_pointer"]["independent_market_evidence"] is False
    assert result["frozen_pointer"]["source_class"] == lineage.PROVENANCE_SCOPE
    assert result["trading_authority_allowed"] is False
    assert result["profitability_proven"] is False
    assert "ACTUAL_EXCHANGE_MARK_PRICE" in result["unproven"]
    assert result["source_sha256"] == DIGESTS


def test_original_canonical_digest_is_stable_and_domain_separated():
    one = lineage.build()
    two = lineage.build()
    assert one == two
    digest = one.pop("dossier_sha256")
    serialized = json.dumps(
        one, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
    ).encode("utf-8")
    assert digest == hashlib.sha256(lineage.DIGEST_PREFIX + serialized).hexdigest()
    assert digest != hashlib.sha256(serialized).hexdigest()


@pytest.mark.parametrize("field", [
    "net_pnl_usdt", "reference_equity_usdt", "net_return_fraction_approx",
    "eligible_signals", "plan_eligible_signals", "portfolio_trades",
    "wins", "losses", "breakeven", "target_first", "stop_first", "neither",
])
def test_strategy_pointer_disagreement_fail_closed(field):
    altered = copy.deepcopy(POINTER)
    if isinstance(altered[field], int):
        altered[field] += 1
    else:
        altered[field] = "987654.32"
    with pytest.raises(EvidenceLineageError, match="disagrees"):
        _dossier(pointer=altered)


def test_pointer_type_spoofing_cannot_equal_reference():
    altered = copy.deepcopy(POINTER)
    altered["wins"] = "36"
    with pytest.raises(EvidenceLineageError, match="disagrees"):
        _dossier(pointer=altered)


@pytest.mark.parametrize("name,value", [
    ("promotion_allowed", True),
    ("strategy_status", "PROMOTED"),
    ("evidence_kind", "INDEPENDENT_LIVE_PROOF"),
    ("source_repository_sha", "invalid"),
    ("source_paths", ["anything_else.json"]),
])
def test_pointer_authority_is_not_trusted(name, value):
    altered = copy.deepcopy(POINTER)
    altered[name] = value
    with pytest.raises(EvidenceLineageError, match="pointer"):
        _dossier(pointer=altered)


def test_source_evidence_path_cannot_be_swapped_silently():
    altered = copy.deepcopy(STATUS)
    altered["strategy"]["evidence_source"] = "research/fabricated-history.json"
    with pytest.raises(EvidenceLineageError, match="path unexpectedly changed"):
        _dossier(status=altered)


def test_trading_rights_cannot_be_granted_by_lineage():
    altered = copy.deepcopy(STATUS)
    altered["trading_authority"]["live_trading_allowed"] = True
    with pytest.raises(EvidenceLineageError):
        _dossier(status=altered)


def test_direct_dependency_to_blocked_mark_price_is_explained():
    changed = copy.deepcopy(REGISTER)
    feature = next(
        row for row in changed["capabilities"]
        if row["id"] == "PORTFOLIO_RISK_SCENARIOS"
    )
    feature["depends_on"].append("MARK_PRICE_FUNDING_PROVENANCE")
    impact = lineage.dependency_impact(changed, root=ROOT)
    match = next(
        row for row in impact["downstream_blocked_capabilities"]
        if row["capability"] == "PORTFOLIO_RISK_SCENARIOS"
    )
    assert match["source_blockers"] == ["MARK_PRICE_FUNDING_PROVENANCE"]
    assert match["distance"] == 1
    assert impact["claim_of_commercial_readiness"] is False


def test_transitive_dependency_blockage_tracks_shortest_distance():
    changed = copy.deepcopy(REGISTER)
    rows = {row["id"]: row for row in changed["capabilities"]}
    rows["PORTFOLIO_RISK_SCENARIOS"]["depends_on"].append(
        "MARK_PRICE_FUNDING_PROVENANCE"
    )
    rows["GRID_DCA_SCENARIO_LAB"]["depends_on"].append(
        "PORTFOLIO_RISK_SCENARIOS"
    )
    impact = lineage.dependency_impact(changed, root=ROOT)
    results = {
        row["capability"]: row for row in impact["downstream_blocked_capabilities"]
    }
    assert results["GRID_DCA_SCENARIO_LAB"]["distance"] == 2
    assert results["GRID_DCA_SCENARIO_LAB"]["source_blockers"] == [
        "MARK_PRICE_FUNDING_PROVENANCE"
    ]


def test_cyclic_dependency_graph_is_rejected_before_impact_analysis():
    changed = copy.deepcopy(REGISTER)
    rows = {row["id"]: row for row in changed["capabilities"]}
    rows["ANTI_LOOKAHEAD_RESEARCH"]["depends_on"].append(
        "GRID_DCA_SCENARIO_LAB"
    )
    with pytest.raises(ValueError, match="cycle"):
        lineage.dependency_impact(changed, root=ROOT)


def test_root_source_digest_missing_is_not_replaced_with_fake_value():
    bad = dict(DIGESTS)
    bad.pop("strategy_pointer")
    with pytest.raises(EvidenceLineageError, match="digest"):
        _dossier(digests=bad)


def test_hashed_dossier_changes_when_source_digest_changes():
    bad = dict(DIGESTS)
    bad["status"] = "a" * 64
    normal = _dossier()
    alternative = _dossier(digests=bad)
    assert alternative["dossier_sha256"] != normal["dossier_sha256"]
    assert alternative["source_sha256"]["status"] == "a" * 64
    assert alternative["decision"] == "RESEARCH_HOLD"


def test_duplicate_json_key_is_rejected(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"win_rate":42,"win_rate":43}', encoding="utf-8")
    with pytest.raises(EvidenceLineageError, match="duplicate"):
        lineage._read_strict(path, tmp_path)


def test_symlinked_source_is_rejected(tmp_path):
    path = tmp_path / "source.json"
    path.write_text("{}")
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(EvidenceLineageError, match="untrusted"):
        lineage._read_strict(link, tmp_path)


def test_source_outside_repository_is_rejected(tmp_path):
    sibling = tmp_path.parent / "unauthorized-evidence.json"
    with pytest.raises(EvidenceLineageError, match="outside repository"):
        lineage._read_strict(sibling, tmp_path)


def test_no_network_or_trading_core_imports():
    source = (ROOT / "tools/research_evidence_lineage.py").read_text()
    for forbidden in (
        "app.oms", "app.execution", "app.risk", "subprocess.",
        "requests.", "httpx.", "urllib.request", "socket.",
    ):
        assert forbidden not in source


def test_cli_output_can_be_checked_for_drift(tmp_path, capsys):
    output = tmp_path / "evidence.json"
    assert lineage.main(["--emit", str(output)]) == 0
    assert lineage.main(["--emit", str(output), "--check"]) == 0
    output.write_text("tampered content")
    assert lineage.main(["--emit", str(output), "--check"]) == 1
    assert "ASTRA_EVIDENCE_DOSSIER_DRIFT" in capsys.readouterr().err
