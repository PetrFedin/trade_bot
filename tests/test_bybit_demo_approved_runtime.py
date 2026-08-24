from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from app.execution import bybit_demo_approved_runtime as approved_runtime
from app.execution.bybit_demo import BybitDemoOrderAck, BybitDemoOrderRequest
from app.execution.bybit_demo_approval_lineage_store import (
    BybitDemoApprovedEntryAuthorizationReceipt,
)
from app.execution.bybit_demo_cycle import BybitDemoCyclePolicy
from app.execution.bybit_demo_operator_approval import BybitDemoOperatorApproval
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


def _instrument() -> BybitInstrumentSpec:
    return BybitInstrumentSpec(
        symbol="BTCUSDT",
        status="Trading",
        contract_type="LinearPerpetual",
        base_coin="BTC",
        quote_coin="USDT",
        settle_coin="USDT",
        tick_size=Decimal("0.1"),
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


class _AuthorizationStore:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    order_submission_supported = False
    immutable_records = True
    outcome_storage_allowed = False
    realized_pnl_storage_allowed = False

    def __init__(
        self,
        events: list[str],
        *,
        fail: bool = False,
        existing: bool = False,
    ) -> None:
        self.events = events
        self.fail = fail
        self.existing = existing
        self.persist_calls = 0

    def persist(self, authorization):
        self.persist_calls += 1
        self.events.append("persist_authorization")
        if self.fail:
            raise RuntimeError("fsync failed")
        return BybitDemoApprovedEntryAuthorizationReceipt(
            entry_order_link_id=authorization.expected_entry_order_link_id,
            approval_id=authorization.approval_id,
            record_sha256="f" * 64,
            idempotent_existing_record=self.existing,
        )


class _RawDemoClient:
    environment = "BYBIT_DEMO"
    live_mainnet_order_routing_allowed = False

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def place_market_order(self, request: BybitDemoOrderRequest) -> BybitDemoOrderAck:
        self.events.append("raw_network")
        return BybitDemoOrderAck(
            order_id="OID-1",
            order_link_id=request.order_link_id,
            accepted=True,
        )


def _call(
    monkeypatch,
    *,
    store: _AuthorizationStore,
    events: list[str],
    now: datetime = _APPROVED,
    canonical_runtime,
):
    monkeypatch.setattr(
        approved_runtime,
        "dry_check_approved_opportunity_matches_demo_selector",
        lambda *args, **kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        approved_runtime,
        "run_bybit_demo_trading_runtime",
        canonical_runtime,
    )
    return approved_runtime.run_operator_approved_bybit_demo_trading_runtime(
        _approval(),
        _review_row(),
        {},
        instruments={"BTCUSDT": _instrument()},
        strategy_config=CryptoPerpStrategyConfig(),
        session_state=_session(),
        now=now,
        now_ms=int(now.timestamp() * 1000),
        client=_RawDemoClient(events),
        accounting_client=SimpleNamespace(
            live_mainnet_order_routing_allowed=False,
            order_writes_supported=False,
        ),
        excursion_store=SimpleNamespace(
            live_mainnet_order_routing_allowed=False,
            order_writes_supported=False,
        ),
        completed_bar_client=SimpleNamespace(live_mainnet_order_routing_allowed=False),
        quote_client=SimpleNamespace(live_mainnet_order_routing_allowed=False),
        runtime_lease=SimpleNamespace(),
        approval_authorization_store=store,
        session_ledger=SimpleNamespace(),
        cycle_policy=BybitDemoCyclePolicy(writes_enabled=True),
    )


def _invoke_entry(kwargs: dict[str, Any]):
    return kwargs["entry_executor"](
        {},
        instruments=kwargs["instruments"],
        strategy_config=kwargs["strategy_config"],
        session_state=kwargs["session_state"],
        now=kwargs["now"],
        client=kwargs["client"],
        accounting_client=kwargs["accounting_client"],
        excursion_store=kwargs["excursion_store"],
        quote_client=kwargs["quote_client"],
        session_ledger=kwargs["session_ledger"],
        cycle_policy=kwargs["cycle_policy"],
    )


def _approved_entry_request() -> BybitDemoOrderRequest:
    approval = _approval()
    return BybitDemoOrderRequest(
        symbol=approval.symbol,
        side="Buy",
        quantity=Decimal("1"),
        order_link_id=approval.expected_entry_order_link_id,
    )


def test_authorization_is_durable_immediately_before_raw_network(monkeypatch) -> None:
    events: list[str] = []
    store = _AuthorizationStore(events)

    def bridge(*args: Any, **kwargs: Any):
        events.append("approved_bridge")
        kwargs["client"].place_market_order(_approved_entry_request())
        return SimpleNamespace(
            live_mainnet_order_routing_allowed=False,
            strategy_cycle_result=None,
        )

    def canonical(*args: Any, **kwargs: Any):
        result = _invoke_entry(kwargs)
        assert result.live_mainnet_order_routing_allowed is False
        return SimpleNamespace(live_mainnet_order_routing_allowed=False)

    monkeypatch.setattr(
        approved_runtime,
        "execute_operator_approved_account_sized_bybit_demo_cycle",
        bridge,
    )
    result = _call(
        monkeypatch,
        store=store,
        events=events,
        canonical_runtime=canonical,
    )

    assert events == ["approved_bridge", "persist_authorization", "raw_network"]
    assert result.authorization_persisted is True
    assert result.authorization is not None
    assert result.authorization_receipt is not None
    assert result.authorization.approval_id == _approval().approval_id
    assert result.live_mainnet_order_routing_allowed is False


def test_pre_order_block_does_not_burn_authorization(monkeypatch) -> None:
    events: list[str] = []
    store = _AuthorizationStore(events)

    def bridge(*args: Any, **kwargs: Any):
        events.append("approved_bridge_blocked_before_order")
        return SimpleNamespace(
            live_mainnet_order_routing_allowed=False,
            strategy_cycle_result=None,
        )

    def canonical(*args: Any, **kwargs: Any):
        _invoke_entry(kwargs)
        return SimpleNamespace(live_mainnet_order_routing_allowed=False)

    monkeypatch.setattr(
        approved_runtime,
        "execute_operator_approved_account_sized_bybit_demo_cycle",
        bridge,
    )
    result = _call(
        monkeypatch,
        store=store,
        events=events,
        canonical_runtime=canonical,
    )

    assert events == ["approved_bridge_blocked_before_order"]
    assert store.persist_calls == 0
    assert result.authorization is not None
    assert result.authorization_persisted is False


def test_authorization_persistence_failure_prevents_raw_network(monkeypatch) -> None:
    events: list[str] = []
    store = _AuthorizationStore(events, fail=True)

    def bridge(*args: Any, **kwargs: Any):
        events.append("approved_bridge")
        kwargs["client"].place_market_order(_approved_entry_request())
        raise AssertionError("persistence failure must interrupt bridge")

    def canonical(*args: Any, **kwargs: Any):
        try:
            _invoke_entry(kwargs)
        except RuntimeError:
            pass
        return SimpleNamespace(live_mainnet_order_routing_allowed=False)

    monkeypatch.setattr(
        approved_runtime,
        "execute_operator_approved_account_sized_bybit_demo_cycle",
        bridge,
    )
    result = _call(
        monkeypatch,
        store=store,
        events=events,
        canonical_runtime=canonical,
    )

    assert events == ["approved_bridge", "persist_authorization"]
    assert "raw_network" not in events
    assert result.authorization is not None
    assert result.authorization_persisted is False


def test_existing_authorization_is_recovery_state_not_resubmit_permission(monkeypatch) -> None:
    events: list[str] = []
    store = _AuthorizationStore(events, existing=True)

    def bridge(*args: Any, **kwargs: Any):
        events.append("approved_bridge")
        kwargs["client"].place_market_order(_approved_entry_request())
        raise AssertionError("existing authorization must interrupt bridge")

    def canonical(*args: Any, **kwargs: Any):
        try:
            _invoke_entry(kwargs)
        except ValueError as exc:
            assert "reconcile before any resubmit" in str(exc)
        return SimpleNamespace(live_mainnet_order_routing_allowed=False)

    monkeypatch.setattr(
        approved_runtime,
        "execute_operator_approved_account_sized_bybit_demo_cycle",
        bridge,
    )
    result = _call(
        monkeypatch,
        store=store,
        events=events,
        canonical_runtime=canonical,
    )

    assert events == ["approved_bridge", "persist_authorization"]
    assert "raw_network" not in events
    assert result.authorization is not None
    assert result.authorization_persisted is False


def test_active_trade_management_does_not_require_fresh_entry_approval(monkeypatch) -> None:
    events: list[str] = []
    store = _AuthorizationStore(events)

    def canonical(*args: Any, **kwargs: Any):
        assert "entry_executor" in kwargs
        return SimpleNamespace(live_mainnet_order_routing_allowed=False)

    result = _call(
        monkeypatch,
        store=store,
        events=events,
        now=_APPROVED + timedelta(hours=1),
        canonical_runtime=canonical,
    )

    assert store.persist_calls == 0
    assert events == []
    assert result.authorization is None
    assert result.authorization_persisted is False
