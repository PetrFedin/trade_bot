from decimal import Decimal

from app.execution.bybit_demo import BybitDemoPosition
from app.execution.bybit_demo_reconciliation import (
    BybitDemoReconciliationStatus,
    aggregate_bybit_demo_executions,
    reconcile_bybit_demo_snapshot,
)
from app.strategy.crypto_perp import CryptoSide, CryptoTradePlan


def _plan(side: CryptoSide = CryptoSide.LONG) -> CryptoTradePlan:
    return CryptoTradePlan(
        symbol="BTCUSDT",
        side=side,
        decision_time="2026-08-12T18:00:00+00:00",
        reference_price=Decimal("100000"),
        notional_usdt=Decimal("1200"),
        reference_quantity=Decimal("0.012"),
        risk_budget_usdt=Decimal("10"),
        stop_fraction=Decimal("0.004"),
        estimated_round_trip_cost_usdt=Decimal("2"),
        estimated_stop_loss_after_cost_usdt=Decimal("7"),
        target_net_profit_usd=Decimal("20"),
        required_move_fraction=Decimal("0.02"),
        expected_move_fraction=Decimal("0.03"),
        expected_net_edge_usd=Decimal("25"),
        quality_score=Decimal("2.5"),
    )


def test_execution_rows_form_weighted_fill_evidence() -> None:
    fill = aggregate_bybit_demo_executions(
        (
            {
                "symbol": "BTCUSDT",
                "side": "Buy",
                "orderLinkId": "ASTRA-DEMO-E-ABC",
                "execQty": "0.004",
                "execPrice": "100000",
                "execFee": "0.24",
            },
            {
                "symbol": "BTCUSDT",
                "side": "Buy",
                "orderLinkId": "ASTRA-DEMO-E-ABC",
                "execQty": "0.008",
                "execPrice": "100100",
                "execFee": "0.48",
            },
        ),
        expected_symbol="BTCUSDT",
        expected_side="Buy",
        expected_order_link_id="ASTRA-DEMO-E-ABC",
    )

    assert fill is not None
    assert fill.execution_count == 2
    assert fill.filled_quantity == Decimal("0.012")
    assert fill.weighted_average_price == Decimal("100066.6666666666666666666667")
    assert fill.execution_fee == Decimal("0.72")


def test_fill_evidence_does_not_substitute_for_position_confirmation() -> None:
    fill = aggregate_bybit_demo_executions(
        (
            {
                "symbol": "BTCUSDT",
                "side": "Buy",
                "orderLinkId": "ASTRA-DEMO-E-ABC",
                "execQty": "0.012",
                "execPrice": "100000",
            },
        ),
        expected_symbol="BTCUSDT",
        expected_side="Buy",
        expected_order_link_id="ASTRA-DEMO-E-ABC",
    )

    reconciled = reconcile_bybit_demo_snapshot(_plan(), positions=(), fill=fill)

    assert reconciled.status is BybitDemoReconciliationStatus.FILL_EVIDENCE_POSITION_PENDING
    assert reconciled.position is None
    assert reconciled.fill is fill
    assert reconciled.next_entry_allowed is False


def test_confirmed_expected_side_position_unlocks_protection_reconciliation() -> None:
    position = BybitDemoPosition(
        symbol="BTCUSDT",
        side="Buy",
        size=Decimal("0.012"),
        average_price=Decimal("100050"),
        unrealised_pnl=Decimal("1"),
    )

    reconciled = reconcile_bybit_demo_snapshot(_plan(), positions=(position,), fill=None)

    assert reconciled.status is BybitDemoReconciliationStatus.POSITION_CONFIRMED
    assert reconciled.position is position
    assert reconciled.live_mainnet_order_routing_allowed is False


def test_opposite_side_position_is_explicit_mismatch_not_expected_fill() -> None:
    position = BybitDemoPosition(
        symbol="BTCUSDT",
        side="Sell",
        size=Decimal("0.012"),
        average_price=Decimal("100050"),
        unrealised_pnl=Decimal("-1"),
    )

    reconciled = reconcile_bybit_demo_snapshot(_plan(), positions=(position,), fill=None)

    assert reconciled.status is BybitDemoReconciliationStatus.POSITION_SIDE_MISMATCH
    assert reconciled.position is position
    assert reconciled.next_entry_allowed is False
