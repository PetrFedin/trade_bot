from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.execution.bybit_demo import BybitDemoPosition
from app.execution.bybit_demo_excursion_runtime import BybitDemoExcursionRuntimeStatus
from app.execution.bybit_demo_excursion_store import JsonFileBybitDemoExcursionStore
from app.execution.bybit_demo_orchestrator import BybitDemoOrchestratorStatus
from app.execution.bybit_demo_ranked_fallback import (
    execute_resilient_account_sized_reconciled_guarded_bybit_demo_cycle,
)
from app.execution.bybit_demo_strategy_selector import BybitDemoStrategyCycleStatus
from app.strategy.crypto_perp import CryptoSide, CryptoTradePlan


def _plan() -> CryptoTradePlan:
    return CryptoTradePlan(
        symbol="BTCUSDT",
        side=CryptoSide.LONG,
        decision_time="2026-08-18T20:00:00+00:00",
        reference_price=Decimal("100"),
        notional_usdt=Decimal("200"),
        reference_quantity=Decimal("2"),
        risk_budget_usdt=Decimal("10"),
        stop_fraction=Decimal("0.05"),
        estimated_round_trip_cost_usdt=Decimal("1"),
        estimated_stop_loss_after_cost_usdt=Decimal("11"),
        target_net_profit_usd=Decimal("20"),
        required_move_fraction=Decimal("0.105"),
        expected_move_fraction=Decimal("0.15"),
        expected_net_edge_usd=Decimal("29"),
        quality_score=Decimal("2"),
    )


def _protected_strategy_cycle():
    cycle = SimpleNamespace(
        status=SimpleNamespace(value="PROTECTED"),
        entry_ack=SimpleNamespace(order_link_id="ASTRA-DEMO-E-RANKED-EXCUR"),
        reconciled_position=BybitDemoPosition(
            symbol="BTCUSDT",
            side="Buy",
            size=Decimal("2"),
            average_price=Decimal("100"),
            unrealised_pnl=Decimal("0"),
            liquidation_price=Decimal("50"),
        ),
    )
    return SimpleNamespace(
        status=BybitDemoStrategyCycleStatus.GUARDED_ORCHESTRATOR_CALLED,
        selection=SimpleNamespace(selected_trade_plan=_plan()),
        orchestrator_result=SimpleNamespace(
            status=BybitDemoOrchestratorStatus.CYCLE_EXECUTED,
            cycle_result=cycle,
        ),
        pre_entry_quote_reasons=(),
        pre_entry_quote_price=Decimal("100"),
        pre_entry_modeled_entry_price=Decimal("100"),
        live_mainnet_order_routing_allowed=False,
    )


def _no_trade_strategy_cycle():
    return SimpleNamespace(
        status=BybitDemoStrategyCycleStatus.NO_TRADE,
        selection=SimpleNamespace(selected_trade_plan=None),
        orchestrator_result=None,
        pre_entry_quote_reasons=(),
        pre_entry_quote_price=None,
        pre_entry_modeled_entry_price=None,
        live_mainnet_order_routing_allowed=False,
    )


def _account_executor_for(strategy_result):
    def executor(*args: object, **kwargs: object):
        cycle = kwargs["strategy_cycle_executor"](
            args[0],
            instruments=kwargs["instruments"],
            strategy_config=kwargs["strategy_config"],
            session_state=kwargs["session_state"],
            now=kwargs["now"],
            client=kwargs["client"],
        )
        return SimpleNamespace(
            strategy_cycle_result=cycle,
            live_mainnet_order_routing_allowed=False,
        )

    def base_strategy(*_args: object, **_kwargs: object):
        return strategy_result

    return executor, base_strategy


def _execute(strategy_result, *, excursion_store=None):
    account_executor, base_strategy = _account_executor_for(strategy_result)
    return execute_resilient_account_sized_reconciled_guarded_bybit_demo_cycle(
        {},
        instruments={"BTCUSDT": object()},
        strategy_config=object(),
        session_state=object(),
        now=object(),
        client=object(),
        accounting_client=object(),
        excursion_store=excursion_store,
        account_sized_executor=account_executor,
        base_strategy_executor=base_strategy,
    )


def test_resilient_cycle_attaches_persistent_excursion_baseline_after_protected_trade(
    tmp_path,
) -> None:
    store = JsonFileBybitDemoExcursionStore(tmp_path / "excursion.json")

    result = _execute(_protected_strategy_cycle(), excursion_store=store)

    assert result.final_selected_symbol == "BTCUSDT"
    assert result.excursion_tracking_result is not None
    assert (
        result.excursion_tracking_result.status
        is BybitDemoExcursionRuntimeStatus.TRACKING_INITIALIZED
    )
    checkpoint = store.load()
    assert checkpoint.entry_order_link_id == "ASTRA-DEMO-E-RANKED-EXCUR"
    assert checkpoint.state.entry_price == Decimal("100")
    assert checkpoint.state.initial_quantity == Decimal("2")


def test_excursion_initialize_failure_is_visible_without_rewriting_trade_result(tmp_path) -> None:
    store = JsonFileBybitDemoExcursionStore(tmp_path / "excursion.json")
    first = _execute(_protected_strategy_cycle(), excursion_store=store)
    assert first.excursion_tracking_result is not None
    assert first.excursion_tracking_result.checkpoint is not None
    first_revision = first.excursion_tracking_result.checkpoint.revision

    second = _execute(_protected_strategy_cycle(), excursion_store=store)

    assert second.final_selected_symbol == "BTCUSDT"
    assert second.account_sized_result.strategy_cycle_result.status is (
        BybitDemoStrategyCycleStatus.GUARDED_ORCHESTRATOR_CALLED
    )
    assert second.excursion_tracking_result is not None
    assert second.excursion_tracking_result.status is BybitDemoExcursionRuntimeStatus.TRACKING_BLOCKED
    assert second.excursion_tracking_result.reasons == (
        "EXCURSION_CHECKPOINT_INITIALIZE_FAILED:FileExistsError",
    )
    assert store.load().revision == first_revision


def test_no_trade_does_not_create_excursion_checkpoint(tmp_path) -> None:
    store = JsonFileBybitDemoExcursionStore(tmp_path / "excursion.json")

    result = _execute(_no_trade_strategy_cycle(), excursion_store=store)

    assert result.final_selected_symbol is None
    assert result.excursion_tracking_result is not None
    assert result.excursion_tracking_result.status is BybitDemoExcursionRuntimeStatus.NOT_APPLICABLE
    with pytest.raises(FileNotFoundError):
        store.load()


def test_absent_excursion_store_keeps_existing_resilient_behavior() -> None:
    result = _execute(_protected_strategy_cycle())

    assert result.final_selected_symbol == "BTCUSDT"
    assert result.excursion_tracking_result is None


def test_unsafe_excursion_store_is_rejected_before_account_cycle() -> None:
    class UnsafeStore:
        live_mainnet_order_routing_allowed = True
        order_writes_supported = False

    called = False

    def account_executor(*_args: object, **_kwargs: object):
        nonlocal called
        called = True
        raise AssertionError("unsafe excursion store must fail before account cycle")

    with pytest.raises(ValueError, match="mainnet-capable excursion store"):
        execute_resilient_account_sized_reconciled_guarded_bybit_demo_cycle(
            {},
            instruments={},
            strategy_config=object(),
            session_state=object(),
            now=object(),
            client=object(),
            accounting_client=object(),
            excursion_store=UnsafeStore(),
            account_sized_executor=account_executor,
        )

    assert called is False
