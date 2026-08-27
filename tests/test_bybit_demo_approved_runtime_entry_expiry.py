from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.execution import bybit_demo_approved_runtime as approved_runtime
from app.execution.bybit_demo import BybitDemoOrderAck, BybitDemoOrderRequest
from app.execution.bybit_demo_cycle import BybitDemoCyclePolicy
from app.execution.bybit_demo_operator_approval import BybitDemoOperatorApproval
from app.strategy.crypto_perp import CryptoPerpStrategyConfig
from app.strategy.crypto_session_risk import CryptoSessionRiskState

_DECISION = datetime(2026, 8, 27, 12, tzinfo=UTC)
_APPROVED = _DECISION + timedelta(minutes=6)
_EXPIRED = _APPROVED + timedelta(minutes=2, seconds=1)


def _approval() -> BybitDemoOperatorApproval:
    return BybitDemoOperatorApproval(
        source_snapshot_id="a" * 64,
        source_evidence_rank=1,
        source_market_rank=1,
        symbol="BTCUSDT",
        side="LONG",
        decision_time=_DECISION.isoformat(),
        signal_available_at=(_DECISION + timedelta(minutes=5)).isoformat(),
        signal_quality_score=Decimal("1.5"),
        source_planned_notional_usdt=Decimal("500"),
        source_risk_budget_usdt=Decimal("10"),
        source_modeled_round_trip_cost_usdt=Decimal("1"),
        maximum_entry_quantity=Decimal("1"),
        approved_at=_APPROVED.isoformat(),
        expires_at=(_APPROVED + timedelta(minutes=2)).isoformat(),
    )


def _request(*, reduce_only: bool = False) -> BybitDemoOrderRequest:
    approval = _approval()
    return BybitDemoOrderRequest(
        symbol=approval.symbol,
        side="Sell" if reduce_only else "Buy",
        quantity=Decimal("1"),
        order_link_id=(
            approval.expected_close_order_link_id
            if reduce_only
            else approval.expected_entry_order_link_id
        ),
        reduce_only=reduce_only,
    )


class _RawClient:
    environment = "BYBIT_DEMO"
    live_mainnet_order_routing_allowed = False
    protection_state_read_supported = True

    def __init__(self) -> None:
        self.orders: list[BybitDemoOrderRequest] = []

    def place_market_order(self, request: BybitDemoOrderRequest) -> BybitDemoOrderAck:
        self.orders.append(request)
        return BybitDemoOrderAck("OID-1", request.order_link_id, True)


class _ControlPlane:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    order_submission_supported = False
    immutable_records = True

    def read_decision(self, *, now: datetime):
        return SimpleNamespace(
            live_mainnet_order_routing_allowed=False,
            order_writes_supported=False,
            new_entry_allowed=True,
            reasons=(),
        )


class _AuthorizationStore:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    order_submission_supported = False
    immutable_records = True
    outcome_storage_allowed = False
    realized_pnl_storage_allowed = False


class _PassthroughDurableClient:
    environment = "BYBIT_DEMO"
    live_mainnet_order_routing_allowed = False
    protection_state_read_supported = True

    def __init__(
        self,
        client: Any,
        _approval: Any,
        _authorization: Any,
        *,
        store: Any,
        on_persisted: Any,
    ) -> None:
        self._client = client
        self.store = store
        self.on_persisted = on_persisted
        self.entry_calls = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def place_market_order(self, request: BybitDemoOrderRequest):
        self.entry_calls += 1
        return self._client.place_market_order(request)


def test_runtime_rechecks_expiry_immediately_before_durable_entry_boundary(
    monkeypatch,
) -> None:
    raw = _RawClient()
    observed_errors: list[str] = []
    times = iter((_APPROVED, _EXPIRED))

    monkeypatch.setattr(
        approved_runtime,
        "build_bybit_demo_approved_entry_authorization",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        approved_runtime,
        "dry_check_approved_opportunity_matches_demo_selector",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        approved_runtime,
        "DurableApprovalLineageBybitDemoClient",
        _PassthroughDurableClient,
    )

    def bridge(*_args: Any, **kwargs: Any):
        kwargs["client"].place_market_order(_request())
        raise AssertionError("expired approval must block before durable entry boundary")

    def canonical(*_args: Any, **kwargs: Any):
        try:
            kwargs["entry_executor"](
                {},
                instruments=kwargs["instruments"],
                strategy_config=kwargs["strategy_config"],
                session_state=kwargs["session_state"],
                now=kwargs["now"],
                client=kwargs["client"],
                accounting_client=kwargs["accounting_client"],
                excursion_store=kwargs["excursion_store"],
                session_ledger=kwargs["session_ledger"],
                cycle_policy=kwargs["cycle_policy"],
            )
        except ValueError as exc:
            observed_errors.append(str(exc))
        return SimpleNamespace(live_mainnet_order_routing_allowed=False)

    monkeypatch.setattr(
        approved_runtime,
        "execute_operator_approved_account_sized_bybit_demo_cycle",
        bridge,
    )
    monkeypatch.setattr(approved_runtime, "run_bybit_demo_trading_runtime", canonical)

    result = approved_runtime.run_operator_approved_bybit_demo_trading_runtime(
        _approval(),
        {},
        {},
        instruments={},
        strategy_config=CryptoPerpStrategyConfig(),
        session_state=CryptoSessionRiskState(
            opening_equity_usdt=Decimal("1000"),
            current_equity_usdt=Decimal("1000"),
            peak_equity_usdt=Decimal("1000"),
        ),
        now=_APPROVED,
        now_ms=int(_APPROVED.timestamp() * 1000),
        client=raw,
        accounting_client=SimpleNamespace(),
        excursion_store=SimpleNamespace(),
        completed_bar_client=SimpleNamespace(),
        quote_client=SimpleNamespace(),
        runtime_lease=SimpleNamespace(),
        approval_authorization_store=_AuthorizationStore(),
        new_entry_control_plane=_ControlPlane(),
        control_now_provider=lambda: next(times),
        session_ledger=SimpleNamespace(),
        cycle_policy=BybitDemoCyclePolicy(writes_enabled=True),
    )

    assert result.authorization is not None
    assert result.authorization_persisted is False
    assert observed_errors and "expired" in observed_errors[0].lower()
    assert raw.orders == []


def test_fresh_approval_guard_does_not_block_reduce_only_safety_close() -> None:
    raw = _RawClient()
    guard = approved_runtime._FreshApprovalGuardedBybitDemoClient(
        raw,
        _approval(),
        now_provider=lambda: _EXPIRED,
    )

    guard.place_market_order(_request(reduce_only=True))

    assert len(raw.orders) == 1
    assert raw.orders[0].reduce_only is True


def test_fresh_approval_guard_blocks_expired_entry_without_delegate_call() -> None:
    raw = _RawClient()
    guard = approved_runtime._FreshApprovalGuardedBybitDemoClient(
        raw,
        _approval(),
        now_provider=lambda: _EXPIRED,
    )

    with pytest.raises(ValueError, match="expired"):
        guard.place_market_order(_request())

    assert raw.orders == []
