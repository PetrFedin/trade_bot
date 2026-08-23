from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.execution import bybit_demo_approved_bridge as bridge
from app.execution.bybit_demo_cycle import BybitDemoCyclePolicy
from app.execution.bybit_demo_operator_approval import (
    BybitDemoOperatorApproval,
    OperatorApprovedBybitDemoClient,
)
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.strategy.crypto_perp import CryptoPerpStrategyConfig
from app.strategy.crypto_session_risk import CryptoSessionRiskState

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


def _review_row() -> dict[str, Any]:
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


def _instrument() -> BybitInstrumentSpec:
    return BybitInstrumentSpec(
        symbol="BTCUSDT",
        status="Trading",
        contract_type="LinearPerpetual",
        base_coin="BTC",
        quote_coin="USDT",
        settle_coin="USDT",
        tick_size=Decimal("0.01"),
        min_order_qty=Decimal("0.001"),
        qty_step=Decimal("0.001"),
        min_notional_value=Decimal("5"),
        max_market_order_qty=Decimal("1000"),
        max_leverage=Decimal("100"),
        funding_interval_minutes=480,
    )


def _session() -> CryptoSessionRiskState:
    return CryptoSessionRiskState(
        opening_equity_usdt=Decimal("1000"),
        current_equity_usdt=Decimal("1000"),
        peak_equity_usdt=Decimal("1000"),
    )


class _DemoClient:
    environment = "BYBIT_DEMO"
    live_mainnet_order_routing_allowed = False


class _ReadOnlyAccountingClient:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False


def test_bridge_requires_explicit_demo_write_policy_before_downstream_call(monkeypatch) -> None:
    called = False

    def _downstream(*args: Any, **kwargs: Any):
        nonlocal called
        called = True
        raise AssertionError("downstream should not be called")

    monkeypatch.setattr(
        bridge,
        "execute_account_sized_reconciled_guarded_bybit_demo_cycle",
        _downstream,
    )
    with pytest.raises(ValueError, match="writes_enabled=true"):
        bridge.execute_operator_approved_account_sized_bybit_demo_cycle(
            _approval(),
            _review_row(),
            {},
            instruments={"BTCUSDT": _instrument()},
            strategy_config=CryptoPerpStrategyConfig(),
            session_state=_session(),
            now=_APPROVED,
            client=_DemoClient(),
            accounting_client=_ReadOnlyAccountingClient(),
            session_ledger=SimpleNamespace(),
            cycle_policy=BybitDemoCyclePolicy(writes_enabled=False),
        )
    assert called is False


def test_bridge_passes_only_operator_guarded_demo_client_to_account_runtime(monkeypatch) -> None:
    approval = _approval()
    captured: dict[str, Any] = {}

    def _dry_check(*args: Any, **kwargs: Any):
        captured["dry_check_called"] = True
        return SimpleNamespace()

    def _downstream(*args: Any, **kwargs: Any):
        captured["client"] = kwargs["client"]
        captured["cycle_policy"] = kwargs["cycle_policy"]
        return SimpleNamespace(
            live_mainnet_order_routing_allowed=False,
            strategy_cycle_result=None,
        )

    monkeypatch.setattr(
        bridge,
        "dry_check_approved_opportunity_matches_demo_selector",
        _dry_check,
    )
    monkeypatch.setattr(
        bridge,
        "execute_account_sized_reconciled_guarded_bybit_demo_cycle",
        _downstream,
    )
    result = bridge.execute_operator_approved_account_sized_bybit_demo_cycle(
        approval,
        _review_row(),
        {},
        instruments={"BTCUSDT": _instrument()},
        strategy_config=CryptoPerpStrategyConfig(),
        session_state=_session(),
        now=_APPROVED,
        client=_DemoClient(),
        accounting_client=_ReadOnlyAccountingClient(),
        session_ledger=SimpleNamespace(),
        cycle_policy=BybitDemoCyclePolicy(writes_enabled=True),
    )

    assert captured["dry_check_called"] is True
    assert isinstance(captured["client"], OperatorApprovedBybitDemoClient)
    assert captured["client"].live_mainnet_order_routing_allowed is False
    assert captured["cycle_policy"].writes_enabled is True
    assert result.live_mainnet_order_routing_allowed is False


def test_bridge_rejects_latest_snapshot_drift_before_dry_selector(monkeypatch) -> None:
    called = False

    def _dry_check(*args: Any, **kwargs: Any):
        nonlocal called
        called = True
        raise AssertionError("dry selector should not be called")

    monkeypatch.setattr(
        bridge,
        "dry_check_approved_opportunity_matches_demo_selector",
        _dry_check,
    )
    changed = _review_row()
    changed["snapshot_id"] = "b" * 64
    with pytest.raises(ValueError, match="snapshot_id"):
        bridge.execute_operator_approved_account_sized_bybit_demo_cycle(
            _approval(),
            changed,
            {},
            instruments={"BTCUSDT": _instrument()},
            strategy_config=CryptoPerpStrategyConfig(),
            session_state=_session(),
            now=_APPROVED,
            client=_DemoClient(),
            accounting_client=_ReadOnlyAccountingClient(),
            session_ledger=SimpleNamespace(),
            cycle_policy=BybitDemoCyclePolicy(writes_enabled=True),
        )
    assert called is False


def test_bridge_rejects_any_mainnet_capable_downstream_result(monkeypatch) -> None:
    monkeypatch.setattr(
        bridge,
        "dry_check_approved_opportunity_matches_demo_selector",
        lambda *args, **kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        bridge,
        "execute_account_sized_reconciled_guarded_bybit_demo_cycle",
        lambda *args, **kwargs: SimpleNamespace(
            live_mainnet_order_routing_allowed=True,
            strategy_cycle_result=None,
        ),
    )
    with pytest.raises(ValueError, match="mainnet permission"):
        bridge.execute_operator_approved_account_sized_bybit_demo_cycle(
            _approval(),
            _review_row(),
            {},
            instruments={"BTCUSDT": _instrument()},
            strategy_config=CryptoPerpStrategyConfig(),
            session_state=_session(),
            now=_APPROVED,
            client=_DemoClient(),
            accounting_client=_ReadOnlyAccountingClient(),
            session_ledger=SimpleNamespace(),
            cycle_policy=BybitDemoCyclePolicy(writes_enabled=True),
        )
