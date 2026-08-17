from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.execution.bybit_demo_cycle import (
    BybitDemoCycleResult,
    BybitDemoCycleStatus,
)
from app.execution.bybit_demo_lifecycle_gate import (
    BybitDemoLifecycleDecision,
    BybitDemoLifecycleStatus,
)
from app.execution.bybit_demo_orchestrator import (
    BybitDemoOrchestratorStatus,
    BybitDemoPreviousTradeReference,
    execute_guarded_bybit_demo_cycle,
    execute_reconciled_guarded_bybit_demo_cycle,
)
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.strategy.crypto_perp import CryptoPerpStrategyConfig, CryptoSide, CryptoTradePlan
from app.strategy.crypto_session_risk import CryptoSessionRiskState


def _trade_plan() -> CryptoTradePlan:
    return CryptoTradePlan(
        symbol="BTCUSDT",
        side=CryptoSide.LONG,
        decision_time="2026-08-17T10:00:00+00:00",
        reference_price=Decimal("100000"),
        notional_usdt=Decimal("1000"),
        reference_quantity=Decimal("0.01"),
        risk_budget_usdt=Decimal("10"),
        stop_fraction=Decimal("0.005"),
        estimated_round_trip_cost_usdt=Decimal("1.6"),
        estimated_stop_loss_after_cost_usdt=Decimal("6.6"),
        target_net_profit_usd=Decimal("20"),
        required_move_fraction=Decimal("0.0216"),
        expected_move_fraction=Decimal("0.03"),
        expected_net_edge_usd=Decimal("28.4"),
        quality_score=Decimal("2.0"),
    )


def _instrument() -> BybitInstrumentSpec:
    return BybitInstrumentSpec(
        symbol="BTCUSDT",
        status="Trading",
        contract_type="LinearPerpetual",
        base_coin="BTC",
        quote_coin="USDT",
        settle_coin="USDT",
        tick_size=Decimal("0.10"),
        min_order_qty=Decimal("0.001"),
        qty_step=Decimal("0.001"),
        min_notional_value=Decimal("5"),
        max_market_order_qty=Decimal("500"),
        max_leverage=Decimal("100"),
        funding_interval_minutes=480,
    )


def _session() -> CryptoSessionRiskState:
    return CryptoSessionRiskState(
        opening_equity_usdt=Decimal("1000"),
        current_equity_usdt=Decimal("1000"),
        peak_equity_usdt=Decimal("1000"),
        realized_pnl_usdt=Decimal("0"),
        execution_cost_usdt=Decimal("0"),
        consecutive_losses=0,
    )


def _lifecycle(*, allow: bool) -> BybitDemoLifecycleDecision:
    return BybitDemoLifecycleDecision(
        status=(
            BybitDemoLifecycleStatus.FULLY_RECONCILED
            if allow
            else BybitDemoLifecycleStatus.ACCOUNT_PNL_PENDING
        ),
        reasons=() if allow else ("ACCOUNT_CLOSED_PNL_RECONCILIATION_PENDING",),
        next_entry_allowed=allow,
        trade_terminal=True,
        account_closed_pnl_reconciled=allow,
        funding_reconciled=allow,
        fully_reconciled_net_pnl=allow,
    )


def _successful_cycle(*args: Any, **kwargs: Any) -> BybitDemoCycleResult:
    return BybitDemoCycleResult(
        status=BybitDemoCycleStatus.PROTECTED,
        reasons=(),
        entry_ack=None,
        protection_ack=None,
        flatten_ack=None,
        reconciled_position=None,
        next_entry_allowed=True,
        demo_order_writes_enabled=True,
        live_mainnet_order_routing_allowed=False,
    )


def _previous_trade() -> BybitDemoPreviousTradeReference:
    return BybitDemoPreviousTradeReference(
        symbol="BTCUSDT",
        entry_side="Buy",
        entry_order_link_id="ASTRA-DEMO-E-ABC123",
    )


def test_reconciled_orchestrator_blocks_on_unresolved_previous_trade() -> None:
    cycle_called = False

    def _reconcile(**kwargs: Any) -> Any:
        assert kwargs["trade_client"] == "trade-reader"
        assert kwargs["accounting_client"] == "account-reader"
        assert kwargs["symbol"] == "BTCUSDT"
        return SimpleNamespace(lifecycle=_lifecycle(allow=False))

    def _must_not_run(*args: Any, **kwargs: Any) -> BybitDemoCycleResult:
        nonlocal cycle_called
        cycle_called = True
        raise AssertionError("cycle executor must not run")

    result = execute_reconciled_guarded_bybit_demo_cycle(
        _trade_plan(),
        instrument=_instrument(),
        strategy_config=CryptoPerpStrategyConfig(target_net_profit_usd=Decimal("20")),
        session_state=_session(),
        client=object(),
        previous_trade=_previous_trade(),
        trade_read_client="trade-reader",
        accounting_client="account-reader",
        lifecycle_reconciler=_reconcile,
        cycle_executor=_must_not_run,
    )

    assert cycle_called is False
    assert result.status is (
        BybitDemoOrchestratorStatus.PREVIOUS_TRADE_RECONCILIATION_BLOCKED
    )
    assert result.previous_trade_accounting is not None
    assert result.previous_trade_accounting.lifecycle.next_entry_allowed is False
    assert result.next_entry_allowed is False


def test_reconciled_orchestrator_executes_after_full_previous_accounting() -> None:
    def _reconcile(**kwargs: Any) -> Any:
        assert kwargs["entry_side"] == "Buy"
        assert kwargs["entry_order_link_id"] == "ASTRA-DEMO-E-ABC123"
        return SimpleNamespace(lifecycle=_lifecycle(allow=True))

    result = execute_reconciled_guarded_bybit_demo_cycle(
        _trade_plan(),
        instrument=_instrument(),
        strategy_config=CryptoPerpStrategyConfig(target_net_profit_usd=Decimal("20")),
        session_state=_session(),
        client=object(),
        previous_trade=_previous_trade(),
        trade_read_client="trade-reader",
        accounting_client="account-reader",
        lifecycle_reconciler=_reconcile,
        cycle_executor=_successful_cycle,
    )

    assert result.status is BybitDemoOrchestratorStatus.CYCLE_EXECUTED
    assert result.previous_trade_accounting is not None
    assert result.previous_trade_accounting.lifecycle.fully_reconciled_net_pnl is True
    assert result.previous_trade_gate_checked is True
    assert result.next_entry_allowed is True


def test_reconciled_orchestrator_requires_readers_for_previous_trade() -> None:
    with pytest.raises(ValueError, match="requires trade and accounting readers"):
        execute_reconciled_guarded_bybit_demo_cycle(
            _trade_plan(),
            instrument=_instrument(),
            strategy_config=CryptoPerpStrategyConfig(target_net_profit_usd=Decimal("20")),
            session_state=_session(),
            client=object(),
            previous_trade=_previous_trade(),
            cycle_executor=_successful_cycle,
        )


def test_previous_unreconciled_trade_blocks_before_cycle_executor() -> None:
    called = False

    def _must_not_run(*args: Any, **kwargs: Any) -> BybitDemoCycleResult:
        nonlocal called
        called = True
        raise AssertionError("cycle executor must not run")

    result = execute_guarded_bybit_demo_cycle(
        _trade_plan(),
        instrument=_instrument(),
        strategy_config=CryptoPerpStrategyConfig(target_net_profit_usd=Decimal("20")),
        session_state=_session(),
        client=object(),
        previous_trade_lifecycle=_lifecycle(allow=False),
        cycle_executor=_must_not_run,
    )

    assert called is False
    assert result.status is (
        BybitDemoOrchestratorStatus.PREVIOUS_TRADE_RECONCILIATION_BLOCKED
    )
    assert result.cycle_result is None
    assert result.next_entry_allowed is False
    assert "PREVIOUS_TRADE_LIFECYCLE_BLOCKED" in result.reasons
    assert result.live_mainnet_order_routing_allowed is False


def test_first_trade_can_execute_without_prior_lifecycle_record() -> None:
    result = execute_guarded_bybit_demo_cycle(
        _trade_plan(),
        instrument=_instrument(),
        strategy_config=CryptoPerpStrategyConfig(target_net_profit_usd=Decimal("20")),
        session_state=_session(),
        client=object(),
        cycle_executor=_successful_cycle,
    )

    assert result.status is BybitDemoOrchestratorStatus.CYCLE_EXECUTED
    assert result.previous_trade_gate_checked is False
    assert result.cycle_result is not None
    assert result.next_entry_allowed is True


def test_reconciled_previous_trade_allows_next_cycle() -> None:
    lifecycle = _lifecycle(allow=True)
    assert lifecycle.status is BybitDemoLifecycleStatus.FULLY_RECONCILED
    assert lifecycle.fully_reconciled_net_pnl is True

    result = execute_guarded_bybit_demo_cycle(
        _trade_plan(),
        instrument=_instrument(),
        strategy_config=CryptoPerpStrategyConfig(target_net_profit_usd=Decimal("20")),
        session_state=_session(),
        client=object(),
        previous_trade_lifecycle=lifecycle,
        cycle_executor=_successful_cycle,
    )

    assert result.status is BybitDemoOrchestratorStatus.CYCLE_EXECUTED
    assert result.previous_trade_gate_checked is True
    assert result.next_entry_allowed is True
    assert result.strategy_promotion_allowed is False
