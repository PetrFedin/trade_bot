from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.execution.bybit_demo_approval_lineage import (
    build_bybit_demo_approved_entry_authorization,
)
from app.execution.bybit_demo_approved_trade_attribution import (
    build_bybit_demo_approved_trade_attribution,
)
from app.execution.bybit_demo_operator_approval import BybitDemoOperatorApproval
from app.execution.bybit_demo_trade_attribution import BybitDemoTradeAttribution
from app.strategy.crypto_perp import CryptoSide

_DECISION = datetime(2026, 8, 24, 12, tzinfo=UTC)
_APPROVED = _DECISION + timedelta(minutes=6)


def _approval() -> BybitDemoOperatorApproval:
    return BybitDemoOperatorApproval(
        source_snapshot_id="a" * 64,
        source_evidence_rank=3,
        source_market_rank=4,
        symbol="BTCUSDT",
        side="LONG",
        decision_time=_DECISION.isoformat(),
        signal_available_at=(_DECISION + timedelta(minutes=5)).isoformat(),
        signal_quality_score=Decimal("1.5"),
        source_planned_notional_usdt=Decimal("500"),
        source_risk_budget_usdt=Decimal("10"),
        source_modeled_round_trip_cost_usdt=Decimal("1"),
        maximum_entry_quantity=Decimal("5"),
        approved_at=_APPROVED.isoformat(),
        expires_at=(_APPROVED + timedelta(minutes=2)).isoformat(),
    )


def _review_row() -> dict[str, object]:
    approval = _approval()
    return {
        "snapshot_id": approval.source_snapshot_id,
        "evidence_rank": approval.source_evidence_rank,
        "market_rank": approval.source_market_rank,
        "symbol": approval.symbol,
        "qualification_state": "QUALIFIED_POSITIVE_EVIDENCE",
        "signal_side": approval.side,
        "decision_time": approval.decision_time,
        "signal_quality_score": approval.signal_quality_score,
        "expected_net_edge_usd": Decimal("25"),
        "planned_notional_usdt": approval.source_planned_notional_usdt,
        "risk_budget_usdt": approval.source_risk_budget_usdt,
        "estimated_round_trip_cost_usdt": approval.source_modeled_round_trip_cost_usdt,
        "evidence_sample_sufficient": True,
        "positive_historical_evidence": True,
        "operator_review_required": True,
        "trade_actionable": False,
        "strategy_promotion_allowed": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
    }


def _trade(entry_order_link_id: str, *, symbol: str = "BTCUSDT") -> BybitDemoTradeAttribution:
    return BybitDemoTradeAttribution(
        entry_order_link_id=entry_order_link_id,
        terminal_record_sha256="d" * 64,
        symbol=symbol,
        side=CryptoSide.LONG,
        selected_signal_rank=1,
        executable_candidate_count=3,
        selected_after_fallback=False,
        fallback_attempt_count=0,
        fallback_stages=(),
        pre_entry_quote_resized=False,
        pre_entry_quantity_retention_fraction=Decimal("1"),
        economic_shadow_differs_from_current=False,
        exit_mode="FIXED_20_TARGET",
        expected_net_edge_usd=Decimal("25"),
        risk_budget_usdt=Decimal("10"),
        quality_score=Decimal("1.5"),
        actual_fill_adverse_slippage_bps_vs_modeled_entry=Decimal("2"),
        observed_peak_favorable_r=Decimal("1.8"),
        observed_max_adverse_r=Decimal("-0.2"),
        observed_peak_capture_fraction=Decimal("0.7"),
        giveback_from_observed_peak_to_exit_r=Decimal("0.4"),
        execution_fees_usdt=Decimal("1.2"),
        funding_net_usdt=Decimal("-0.1"),
        all_in_net_pnl_usdt=Decimal("18.7"),
        all_in_edge_realization_fraction=Decimal("0.748"),
        all_in_r_multiple=Decimal("1.87"),
    )


def test_approved_trade_attribution_preserves_evidence_source_and_all_in_result() -> None:
    approval = _approval()
    authorization = build_bybit_demo_approved_entry_authorization(
        approval,
        _review_row(),
        now=_APPROVED,
    )

    joined = build_bybit_demo_approved_trade_attribution(
        authorization,
        _trade(approval.expected_entry_order_link_id),
    )

    assert joined.approval_id == approval.approval_id
    assert joined.source_snapshot_id == approval.source_snapshot_id
    assert joined.source_evidence_rank == 3
    assert joined.source_market_rank == 4
    assert joined.entry_order_link_id == approval.expected_entry_order_link_id
    assert joined.execution_fees_usdt == Decimal("1.2")
    assert joined.funding_net_usdt == Decimal("-0.1")
    assert joined.all_in_net_pnl_usdt == Decimal("18.7")
    assert joined.all_in_r_multiple == Decimal("1.87")
    assert joined.realized_pnl_used_for_online_selection is False
    assert joined.live_mainnet_order_routing_allowed is False


def test_approved_trade_attribution_rejects_different_order_identity() -> None:
    authorization = build_bybit_demo_approved_entry_authorization(
        _approval(),
        _review_row(),
        now=_APPROVED,
    )

    with pytest.raises(ValueError, match="orderLinkId mismatch"):
        build_bybit_demo_approved_trade_attribution(
            authorization,
            _trade("ASTRA-DEMO-DIFFERENT"),
        )
