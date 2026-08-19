from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from app.execution.bybit_demo_entry_provenance import BybitDemoEntryDecisionProvenance
from app.execution.bybit_demo_entry_provenance_store import (
    JsonFileBybitDemoEntryProvenanceStore,
)
from app.execution.bybit_demo_ranked_fallback import (
    BybitDemoCandidateFallbackAttempt,
    BybitDemoCandidateFallbackStage,
)
from app.strategy.crypto_perp import CryptoSide

_ENTRY = "ASTRA-DEMO-E-PROVENANCESTORE"


def _provenance() -> BybitDemoEntryDecisionProvenance:
    return BybitDemoEntryDecisionProvenance(
        entry_order_link_id=_ENTRY,
        symbol="BTCUSDT",
        side=CryptoSide.LONG,
        decision_time="2026-08-19T10:00:00+00:00",
        selected_signal_rank=1,
        executable_candidate_count=2,
        candidate_audit_count=8,
        economic_shadow_selected_symbol="ETHUSDT",
        economic_shadow_selected_side="LONG",
        economic_shadow_differs_from_current=True,
        selected_after_fallback=True,
        fallback_attempts=(
            BybitDemoCandidateFallbackAttempt(
                symbol="ETHUSDT",
                side="LONG",
                stage=BybitDemoCandidateFallbackStage.PRE_ENTRY_QUOTE,
                reasons=("NEXT_OPEN_EXPECTED_NET_EDGE_BELOW_TARGET",),
                quote_price=Decimal("2000"),
                modeled_entry_price=Decimal("2001"),
            ),
        ),
        expected_net_edge_usd=Decimal("29.6"),
        risk_budget_usdt=Decimal("10"),
        quality_score=Decimal("2.4"),
        target_net_profit_usd=Decimal("20"),
        planned_reference_price=Decimal("100"),
        planned_reference_quantity=Decimal("2"),
        planned_notional_usdt=Decimal("200"),
        modeled_round_trip_cost_usdt=Decimal("0.4"),
        pre_entry_quote_price=Decimal("100"),
        pre_entry_modeled_entry_price=Decimal("100"),
        pre_entry_original_quantity=Decimal("2"),
        pre_entry_adjusted_quantity=Decimal("1.8"),
        pre_entry_quote_resized=True,
        pre_entry_quantity_retention_fraction=Decimal("0.9"),
        actual_average_entry_price=Decimal("100.05"),
        actual_filled_quantity=Decimal("1.8"),
        actual_fill_notional_usdt=Decimal("180.09"),
        actual_fill_adverse_slippage_bps_vs_modeled_entry=Decimal("5"),
        account_taker_fee_rate=Decimal("0.00055"),
        exit_mode="FIXED_20_TARGET",
        runner_admission_reasons=("RUNNER_EXPECTED_EDGE_BELOW_ADMISSION_GATE",),
        liquidation_safety_reason="SAFE",
        stop_to_liquidation_r=Decimal("2.5"),
        effective_account_equity_usdt=Decimal("1000"),
        effective_peak_equity_usdt=Decimal("1050"),
        margin_mode="REGULAR_MARGIN",
    )


def _record(root: Path) -> Path:
    rows = list(root.glob("*.json"))
    assert len(rows) == 1
    return rows[0]


def test_entry_provenance_store_is_immutable_outcome_free_and_idempotent(tmp_path: Path) -> None:
    store = JsonFileBybitDemoEntryProvenanceStore(tmp_path / "entry-provenance")

    first = store.persist(_provenance())
    second = store.persist(_provenance())

    assert first.record_sha256 == second.record_sha256
    assert first.idempotent_existing_record is False
    assert second.idempotent_existing_record is True
    assert store.immutable_records is True
    assert store.realized_pnl_storage_allowed is False
    assert store.order_writes_supported is False
    assert store.live_mainnet_order_routing_allowed is False

    envelope = json.loads(_record(store.root).read_text(encoding="utf-8"))
    assert envelope["outcome_free"] is True
    assert envelope["realized_pnl_storage_allowed"] is False
    assert "realized_pnl" not in envelope["record"]
    assert "all_in_net_pnl_usdt" not in envelope["record"]


def test_same_entry_id_with_changed_selection_evidence_cannot_be_overwritten(tmp_path: Path) -> None:
    store = JsonFileBybitDemoEntryProvenanceStore(tmp_path / "entry-provenance")
    store.persist(_provenance())
    changed = replace(_provenance(), selected_signal_rank=2)

    with pytest.raises(RuntimeError, match="entry provenance conflict"):
        store.persist(changed)


def test_tampered_provenance_checksum_is_rejected(tmp_path: Path) -> None:
    store = JsonFileBybitDemoEntryProvenanceStore(tmp_path / "entry-provenance")
    store.persist(_provenance())
    path = _record(store.root)
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["record"]["expected_net_edge_usd"] = "999"
    path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(ValueError, match="checksum mismatch"):
        store.persist(_provenance())


def test_realized_pnl_dependency_is_rejected(tmp_path: Path) -> None:
    store = JsonFileBybitDemoEntryProvenanceStore(tmp_path / "entry-provenance")
    invalid = replace(_provenance(), realized_pnl_used_for_selection=True)

    with pytest.raises(ValueError, match="cannot use realized PnL"):
        store.persist(invalid)


def test_live_capable_provenance_is_rejected(tmp_path: Path) -> None:
    store = JsonFileBybitDemoEntryProvenanceStore(tmp_path / "entry-provenance")
    invalid = replace(_provenance(), live_mainnet_order_routing_allowed=True)

    with pytest.raises(ValueError, match="live-capable provenance"):
        store.persist(invalid)


def test_symlink_provenance_root_is_rejected(tmp_path: Path) -> None:
    real_root = tmp_path / "real"
    real_root.mkdir()
    link_root = tmp_path / "linked"
    link_root.symlink_to(real_root, target_is_directory=True)
    store = JsonFileBybitDemoEntryProvenanceStore(link_root)

    with pytest.raises(ValueError, match="root cannot be a symlink"):
        store.persist(_provenance())
