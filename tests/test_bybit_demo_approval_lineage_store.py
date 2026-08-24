from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.execution.bybit_demo_approval_lineage import (
    build_bybit_demo_approved_entry_authorization,
)
from app.execution.bybit_demo_approval_lineage_store import (
    JsonFileBybitDemoApprovedEntryAuthorizationStore,
)
from app.execution.bybit_demo_operator_approval import BybitDemoOperatorApproval

_DECISION = datetime(2026, 8, 24, 12, tzinfo=UTC)
_APPROVED = _DECISION + timedelta(minutes=6)


def _approval() -> BybitDemoOperatorApproval:
    return BybitDemoOperatorApproval(
        source_snapshot_id="a" * 64,
        source_evidence_rank=1,
        source_market_rank=2,
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


def test_authorization_store_is_idempotent_and_outcome_free(tmp_path) -> None:
    authorization = build_bybit_demo_approved_entry_authorization(
        _approval(),
        _review_row(),
        now=_APPROVED,
    )
    store = JsonFileBybitDemoApprovedEntryAuthorizationStore(tmp_path / "approved")

    first = store.persist(authorization)
    second = store.persist(authorization)
    loaded = store.load(entry_order_link_id=authorization.expected_entry_order_link_id)

    assert first.entry_order_link_id == authorization.expected_entry_order_link_id
    assert first.approval_id == authorization.approval_id
    assert first.idempotent_existing_record is False
    assert second.idempotent_existing_record is True
    assert second.record_sha256 == first.record_sha256
    assert loaded.authorization == authorization
    assert loaded.record_sha256 == first.record_sha256
    assert store.order_writes_supported is False
    assert store.order_submission_supported is False
    assert store.outcome_storage_allowed is False
    assert store.realized_pnl_storage_allowed is False
    assert store.live_mainnet_order_routing_allowed is False


def test_same_order_identity_cannot_be_rebound_to_another_approval(tmp_path) -> None:
    authorization = build_bybit_demo_approved_entry_authorization(
        _approval(),
        _review_row(),
        now=_APPROVED,
    )
    store = JsonFileBybitDemoApprovedEntryAuthorizationStore(tmp_path / "approved")
    store.persist(authorization)
    conflicting = replace(
        authorization,
        approval_id="b" * 64,
        source_snapshot_id="c" * 64,
    )

    with pytest.raises(RuntimeError, match="conflict"):
        store.persist(conflicting)
