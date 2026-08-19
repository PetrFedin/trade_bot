from __future__ import annotations

import hashlib
import json
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

_ENTRY = "ASTRA-DEMO-E-LOADPROV"


def _provenance(entry_order_link_id: str = _ENTRY) -> BybitDemoEntryDecisionProvenance:
    return BybitDemoEntryDecisionProvenance(
        entry_order_link_id=entry_order_link_id,
        symbol="BTCUSDT",
        side=CryptoSide.LONG,
        decision_time="2026-08-19T12:00:00+00:00",
        selected_signal_rank=1,
        executable_candidate_count=2,
        candidate_audit_count=4,
        economic_shadow_selected_symbol="ETHUSDT",
        economic_shadow_selected_side="LONG",
        economic_shadow_differs_from_current=True,
        selected_after_fallback=True,
        fallback_attempts=(
            BybitDemoCandidateFallbackAttempt(
                symbol="ETHUSDT",
                side="LONG",
                stage=BybitDemoCandidateFallbackStage.ACCOUNT_FEE_ECONOMICS,
                reasons=("ACCOUNT_FEE_EXPECTED_NET_PROFIT_BELOW_TARGET",),
                quote_price=Decimal("2000.5"),
                modeled_entry_price=Decimal("2001"),
            ),
        ),
        expected_net_edge_usd=Decimal("27.5"),
        risk_budget_usdt=Decimal("10"),
        quality_score=Decimal("2.2"),
        target_net_profit_usd=Decimal("20"),
        planned_reference_price=Decimal("100"),
        planned_reference_quantity=Decimal("2"),
        planned_notional_usdt=Decimal("200"),
        modeled_round_trip_cost_usdt=Decimal("0.45"),
        pre_entry_quote_price=Decimal("100.1"),
        pre_entry_modeled_entry_price=Decimal("100.12"),
        pre_entry_original_quantity=Decimal("2"),
        pre_entry_adjusted_quantity=Decimal("1.9"),
        pre_entry_quote_resized=True,
        pre_entry_quantity_retention_fraction=Decimal("0.95"),
        actual_average_entry_price=Decimal("100.15"),
        actual_filled_quantity=Decimal("1.9"),
        actual_fill_notional_usdt=Decimal("190.285"),
        actual_fill_adverse_slippage_bps_vs_modeled_entry=Decimal("2.9964043148"),
        account_taker_fee_rate=Decimal("0.00055"),
        exit_mode="FIXED_20_TARGET",
        runner_admission_reasons=("RUNNER_EXPECTED_EDGE_BELOW_ADMISSION_GATE",),
        liquidation_safety_reason="SAFE",
        stop_to_liquidation_r=Decimal("2.3"),
        effective_account_equity_usdt=Decimal("1000"),
        effective_peak_equity_usdt=Decimal("1050"),
        margin_mode="REGULAR_MARGIN",
    )


def _record_path(root: Path, entry_order_link_id: str) -> Path:
    digest = hashlib.sha256(entry_order_link_id.encode()).hexdigest()
    return root / f"{digest}.json"


def _rewrite_with_valid_checksum(path: Path, mutate) -> None:
    envelope = json.loads(path.read_text(encoding="utf-8"))
    mutate(envelope["record"])
    canonical = json.dumps(
        envelope["record"],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    envelope["record_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
    path.write_text(
        json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def test_typed_entry_provenance_round_trip_is_lossless(tmp_path: Path) -> None:
    store = JsonFileBybitDemoEntryProvenanceStore(tmp_path / "entry-provenance")
    provenance = _provenance()

    receipt = store.persist(provenance)
    loaded = store.load(entry_order_link_id=_ENTRY)

    assert loaded.provenance == provenance
    assert loaded.record_sha256 == receipt.record_sha256
    assert loaded.live_mainnet_order_routing_allowed is False


def test_valid_checksum_with_wrong_field_type_is_still_rejected(tmp_path: Path) -> None:
    store = JsonFileBybitDemoEntryProvenanceStore(tmp_path / "entry-provenance")
    store.persist(_provenance())
    path = _record_path(store.root, _ENTRY)
    _rewrite_with_valid_checksum(
        path,
        lambda record: record.__setitem__("selected_signal_rank", "1"),
    )

    with pytest.raises(ValueError, match="invalid integer field:selected_signal_rank"):
        store.load(entry_order_link_id=_ENTRY)


def test_record_under_wrong_entry_hash_cannot_be_joined_to_requested_trade(tmp_path: Path) -> None:
    store = JsonFileBybitDemoEntryProvenanceStore(tmp_path / "entry-provenance")
    store.persist(_provenance())
    original = _record_path(store.root, _ENTRY)
    other_id = "ASTRA-DEMO-E-OTHERLOAD"
    other_path = _record_path(store.root, other_id)
    other_path.write_text(original.read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(ValueError, match="entry orderLinkId mismatch"):
        store.load(entry_order_link_id=other_id)


def test_missing_provenance_is_not_silently_treated_as_empty_decision(tmp_path: Path) -> None:
    store = JsonFileBybitDemoEntryProvenanceStore(tmp_path / "entry-provenance")

    with pytest.raises(FileNotFoundError):
        store.load(entry_order_link_id=_ENTRY)
