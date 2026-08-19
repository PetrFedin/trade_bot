from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

import pytest

from app.execution.bybit_demo import BybitDemoOrderAck, BybitDemoPosition
from app.execution.bybit_demo_cycle import BybitDemoCycleStatus
from app.execution.bybit_demo_entry_provenance import (
    build_bybit_demo_entry_decision_provenance,
)
from app.execution.bybit_demo_ranked_fallback import (
    BybitDemoCandidateFallbackAttempt,
    BybitDemoCandidateFallbackStage,
)
from app.execution.bybit_demo_strategy_selector import BybitDemoStrategyCycleStatus
from app.strategy.crypto_perp import CryptoSide, CryptoTradePlan


@dataclass(frozen=True)
class _Selection:
    selected_trade_plan: CryptoTradePlan | None
    selected_signal_rank: int | None
    executable_candidate_count: int = 2
    candidate_audit: tuple[object, ...] = (object(), object(), object())
    economic_shadow_selected_symbol: str | None = "ETHUSDT"
    economic_shadow_selected_side: str | None = "LONG"
    economic_shadow_differs_from_current: bool = True


@dataclass(frozen=True)
class _Cycle:
    status: BybitDemoCycleStatus
    entry_ack: BybitDemoOrderAck | None
    reconciled_position: BybitDemoPosition | None
    account_taker_fee_rate: Decimal | None
    exit_mode: str | None
    runner_admission_reasons: tuple[str, ...] = ()
    liquidation_safety_reason: str | None = "SAFE"
    stop_to_liquidation_r: Decimal | None = Decimal("2.5")
    live_mainnet_order_routing_allowed: bool = False


@dataclass(frozen=True)
class _Orchestrator:
    cycle_result: _Cycle | None
    live_mainnet_order_routing_allowed: bool = False


@dataclass(frozen=True)
class _Strategy:
    status: BybitDemoStrategyCycleStatus
    selection: _Selection
    orchestrator_result: _Orchestrator | None
    pre_entry_quote_price: Decimal | None = Decimal("100")
    pre_entry_modeled_entry_price: Decimal | None = Decimal("100")
    pre_entry_original_quantity: Decimal | None = Decimal("2")
    pre_entry_adjusted_quantity: Decimal | None = Decimal("1.8")
    pre_entry_quote_resized: bool = True
    live_mainnet_order_routing_allowed: bool = False


@dataclass(frozen=True)
class _Account:
    strategy_cycle_result: _Strategy | None
    effective_session_equity_usdt: Decimal = Decimal("1000")
    effective_peak_equity_usdt: Decimal = Decimal("1050")
    margin_mode: str | None = "REGULAR_MARGIN"
    live_mainnet_order_routing_allowed: bool = False


@dataclass(frozen=True)
class _Resilient:
    account_sized_result: _Account
    fallback_attempts: tuple[BybitDemoCandidateFallbackAttempt, ...] = ()
    selected_after_fallback: bool = False
    live_mainnet_order_routing_allowed: bool = False


def _plan(side: CryptoSide = CryptoSide.LONG) -> CryptoTradePlan:
    return CryptoTradePlan(
        symbol="BTCUSDT",
        side=side,
        decision_time="2026-08-19T10:00:00+00:00",
        reference_price=Decimal("100"),
        notional_usdt=Decimal("200"),
        reference_quantity=Decimal("2"),
        risk_budget_usdt=Decimal("10"),
        stop_fraction=Decimal("0.05"),
        estimated_round_trip_cost_usdt=Decimal("0.4"),
        estimated_stop_loss_after_cost_usdt=Decimal("10.4"),
        target_net_profit_usd=Decimal("20"),
        required_move_fraction=Decimal("0.102"),
        expected_move_fraction=Decimal("0.15"),
        expected_net_edge_usd=Decimal("29.6"),
        quality_score=Decimal("2.4"),
    )


def _result(
    *,
    side: CryptoSide = CryptoSide.LONG,
    actual_entry: str = "100.05",
    strategy_status: BybitDemoStrategyCycleStatus = (
        BybitDemoStrategyCycleStatus.GUARDED_ORCHESTRATOR_CALLED
    ),
    cycle_status: BybitDemoCycleStatus = BybitDemoCycleStatus.PROTECTED,
) -> _Resilient:
    plan = _plan(side)
    position_side = "Buy" if side is CryptoSide.LONG else "Sell"
    position = BybitDemoPosition(
        symbol="BTCUSDT",
        side=position_side,
        size=Decimal("1.8"),
        average_price=Decimal(actual_entry),
        unrealised_pnl=Decimal("0"),
        liquidation_price=Decimal("80") if side is CryptoSide.LONG else Decimal("120"),
    )
    cycle = _Cycle(
        status=cycle_status,
        entry_ack=BybitDemoOrderAck(
            order_id="oid-1",
            order_link_id="ASTRA-DEMO-E-PROVENANCE",
            accepted=True,
        ),
        reconciled_position=position,
        account_taker_fee_rate=Decimal("0.00055"),
        exit_mode="FIXED_20_TARGET",
        runner_admission_reasons=("RUNNER_EXPECTED_EDGE_BELOW_ADMISSION_GATE",),
    )
    strategy = _Strategy(
        status=strategy_status,
        selection=_Selection(selected_trade_plan=plan, selected_signal_rank=1),
        orchestrator_result=_Orchestrator(cycle),
    )
    fallback = BybitDemoCandidateFallbackAttempt(
        symbol="ETHUSDT",
        side="LONG",
        stage=BybitDemoCandidateFallbackStage.PRE_ENTRY_QUOTE,
        reasons=("NEXT_OPEN_EXPECTED_NET_EDGE_BELOW_TARGET",),
        quote_price=Decimal("2000"),
        modeled_entry_price=Decimal("2001"),
    )
    return _Resilient(
        account_sized_result=_Account(strategy),
        fallback_attempts=(fallback,),
        selected_after_fallback=True,
    )


def test_entry_provenance_captures_selection_execution_and_fallback_without_outcome() -> None:
    provenance = build_bybit_demo_entry_decision_provenance(_result())

    assert provenance is not None
    assert provenance.entry_order_link_id == "ASTRA-DEMO-E-PROVENANCE"
    assert provenance.symbol == "BTCUSDT"
    assert provenance.side is CryptoSide.LONG
    assert provenance.selected_signal_rank == 1
    assert provenance.executable_candidate_count == 2
    assert provenance.candidate_audit_count == 3
    assert provenance.economic_shadow_selected_symbol == "ETHUSDT"
    assert provenance.economic_shadow_differs_from_current is True
    assert provenance.selected_after_fallback is True
    assert len(provenance.fallback_attempts) == 1
    assert provenance.expected_net_edge_usd == Decimal("29.6")
    assert provenance.risk_budget_usdt == Decimal("10")
    assert provenance.quality_score == Decimal("2.4")
    assert provenance.pre_entry_quantity_retention_fraction == Decimal("0.9")
    assert provenance.actual_filled_quantity == Decimal("1.8")
    assert provenance.actual_fill_notional_usdt == Decimal("180.090")
    assert provenance.actual_fill_adverse_slippage_bps_vs_modeled_entry == Decimal("5.0000")
    assert provenance.account_taker_fee_rate == Decimal("0.00055")
    assert provenance.exit_mode == "FIXED_20_TARGET"
    assert provenance.realized_pnl_used_for_selection is False
    assert provenance.diagnostics_only is True
    assert provenance.automatic_selector_retuning_allowed is False
    assert provenance.strategy_promotion_allowed is False
    assert provenance.live_mainnet_order_routing_allowed is False


def test_short_adverse_slippage_sign_is_positive_when_fill_is_worse() -> None:
    provenance = build_bybit_demo_entry_decision_provenance(
        _result(side=CryptoSide.SHORT, actual_entry="99.95")
    )

    assert provenance is not None
    assert provenance.side is CryptoSide.SHORT
    assert provenance.actual_fill_adverse_slippage_bps_vs_modeled_entry == Decimal("5.0000")


def test_better_than_modeled_fill_produces_negative_adverse_slippage() -> None:
    provenance = build_bybit_demo_entry_decision_provenance(_result(actual_entry="99.90"))

    assert provenance is not None
    assert provenance.actual_fill_adverse_slippage_bps_vs_modeled_entry == Decimal("-10.000")


def test_unprotected_or_nonexecuted_cycle_has_no_entry_provenance() -> None:
    assert (
        build_bybit_demo_entry_decision_provenance(
            _result(cycle_status=BybitDemoCycleStatus.ENTRY_BLOCKED)
        )
        is None
    )
    assert (
        build_bybit_demo_entry_decision_provenance(
            _result(strategy_status=BybitDemoStrategyCycleStatus.NO_TRADE)
        )
        is None
    )


def test_pre_entry_resize_cannot_claim_quantity_increase() -> None:
    result = _result()
    strategy = result.account_sized_result.strategy_cycle_result
    assert strategy is not None
    invalid_strategy = replace(
        strategy,
        pre_entry_original_quantity=Decimal("1.8"),
        pre_entry_adjusted_quantity=Decimal("2"),
    )
    invalid = replace(
        result,
        account_sized_result=replace(
            result.account_sized_result,
            strategy_cycle_result=invalid_strategy,
        ),
    )

    with pytest.raises(ValueError, match="cannot increase quantity"):
        build_bybit_demo_entry_decision_provenance(invalid)


def test_mainnet_capable_result_is_rejected() -> None:
    unsafe = replace(_result(), live_mainnet_order_routing_allowed=True)

    with pytest.raises(ValueError, match="mainnet-capable resilient result"):
        build_bybit_demo_entry_decision_provenance(unsafe)
