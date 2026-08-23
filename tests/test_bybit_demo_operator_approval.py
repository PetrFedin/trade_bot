from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.execution.bybit_demo import (
    BybitDemoOrderAck,
    BybitDemoOrderRequest,
    BybitDemoProtectionAck,
    BybitDemoProtectionRequest,
)
from app.execution.bybit_demo_operator_approval import (
    OperatorApprovedBybitDemoClient,
    create_bybit_demo_operator_approval,
    dry_check_approved_opportunity_matches_demo_selector,
)
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.marketdata.bybit_v5 import BybitKlineBar
from app.strategy.crypto_perp import CryptoPerpStrategyConfig, build_trade_plan, evaluate_crypto_signal
from app.strategy.crypto_session_risk import CryptoSessionRiskState

_START = datetime(2026, 8, 24, 0, tzinfo=UTC)
_STEP = timedelta(minutes=5)
_COUNT = 120


def _bars(symbol: str, *, trending: bool) -> tuple[BybitKlineBar, ...]:
    rows: list[BybitKlineBar] = []
    for index in range(_COUNT):
        base = Decimal("100") + (Decimal(index) * Decimal("0.35") if trending else Decimal("0"))
        opened = base
        close = opened + (Decimal("0.15") if trending else Decimal("0"))
        rows.append(
            BybitKlineBar(
                symbol=symbol,
                start_time=_START + index * _STEP,
                open=opened,
                high=max(opened, close) + Decimal("0.50"),
                low=min(opened, close) - Decimal("0.50"),
                close=close,
                volume=Decimal("10000"),
                turnover=Decimal("2000000") + Decimal(index * 1000),
            )
        )
    return tuple(rows)


def _instrument(symbol: str) -> BybitInstrumentSpec:
    return BybitInstrumentSpec(
        symbol=symbol,
        status="Trading",
        contract_type="LinearPerpetual",
        base_coin=symbol.removesuffix("USDT"),
        quote_coin="USDT",
        settle_coin="USDT",
        tick_size=Decimal("0.01"),
        min_order_qty=Decimal("0.001"),
        qty_step=Decimal("0.001"),
        min_notional_value=Decimal("5"),
        max_market_order_qty=Decimal("100000"),
        max_leverage=Decimal("100"),
        funding_interval_minutes=480,
    )


def _source_row() -> tuple[dict[str, Any], tuple[BybitKlineBar, ...]]:
    bars = _bars("C00USDT", trending=True)
    config = CryptoPerpStrategyConfig()
    evaluation = evaluate_crypto_signal(bars, config)
    assert evaluation.eligible is True
    assert evaluation.signal is not None
    signal = evaluation.signal
    plan_evaluation = build_trade_plan(
        signal,
        equity_usdt=Decimal("1000"),
        config=config,
    )
    assert plan_evaluation.eligible is True
    assert plan_evaluation.plan is not None
    plan = plan_evaluation.plan
    return (
        {
            "snapshot_id": "a" * 64,
            "evidence_rank": 1,
            "market_rank": 2,
            "symbol": plan.symbol,
            "qualification_state": "QUALIFIED_POSITIVE_EVIDENCE",
            "qualification_reasons": [],
            "signal_side": plan.side.value,
            "decision_time": plan.decision_time,
            "signal_quality_score": plan.quality_score,
            "expected_net_edge_usd": plan.expected_net_edge_usd,
            "planned_notional_usdt": plan.notional_usdt,
            "risk_budget_usdt": plan.risk_budget_usdt,
            "estimated_round_trip_cost_usdt": plan.estimated_round_trip_cost_usdt,
            "evidence_sample_sufficient": True,
            "positive_historical_evidence": True,
            "operator_review_required": True,
            "trade_actionable": False,
            "strategy_promotion_allowed": False,
            "demo_activation_allowed": False,
            "live_activation_allowed": False,
            "bybit_live_order_routing_allowed": False,
        },
        bars,
    )


def _session() -> CryptoSessionRiskState:
    return CryptoSessionRiskState(
        opening_equity_usdt=Decimal("1000"),
        current_equity_usdt=Decimal("1000"),
        peak_equity_usdt=Decimal("1000"),
    )


class _FakeDemoClient:
    environment = "BYBIT_DEMO"
    live_mainnet_order_routing_allowed = False

    def __init__(self) -> None:
        self.orders: list[BybitDemoOrderRequest] = []
        self.protections: list[BybitDemoProtectionRequest] = []

    def place_market_order(self, request: BybitDemoOrderRequest) -> BybitDemoOrderAck:
        self.orders.append(request)
        return BybitDemoOrderAck("order-1", request.order_link_id, True)

    def set_full_position_protection(self, request: BybitDemoProtectionRequest):
        self.protections.append(request)
        return BybitDemoProtectionAck(
            symbol=request.symbol,
            take_profit_price=request.take_profit_price,
            stop_loss_price=request.stop_loss_price,
        )

    def get_fee_rate(self, *, symbol: str):
        return ("fee", symbol)

    def get_positions(self, *, settle_coin: str = "USDT"):
        return ("positions", settle_coin)

    def get_executions(self, *, symbol: str, order_link_id=None, limit: int = 50):
        return (symbol, order_link_id, limit)

    def cancel_order(self, *, symbol: str, order_link_id: str):
        return BybitDemoOrderAck("cancel-1", order_link_id, True)


def test_approval_requires_exact_phrase_and_fresh_positive_evidence() -> None:
    row, bars = _source_row()
    decision = datetime.fromisoformat(row["decision_time"])
    approved_at = decision + timedelta(minutes=6)

    with pytest.raises(ValueError, match="confirmation phrase"):
        create_bybit_demo_operator_approval(
            row,
            bars,
            approved_at=approved_at,
            confirmation_phrase="YES",
        )

    approval = create_bybit_demo_operator_approval(
        row,
        bars,
        approved_at=approved_at,
        confirmation_phrase="APPROVE_BYBIT_DEMO_EXECUTION",
    )
    assert approval.environment == "BYBIT_DEMO"
    assert approval.operator_confirmed is True
    assert approval.live_mainnet_order_routing_allowed is False
    assert len(approval.approval_id) == 64
    assert approval.maximum_entry_quantity > 0

    stale = approved_at + timedelta(minutes=11)
    with pytest.raises(ValueError, match="stale"):
        create_bybit_demo_operator_approval(
            row,
            bars,
            approved_at=stale,
            confirmation_phrase="APPROVE_BYBIT_DEMO_EXECUTION",
        )

    unsafe = dict(row)
    unsafe["qualification_state"] = "QUALIFIED_MIXED_EVIDENCE"
    with pytest.raises(ValueError, match="QUALIFIED_POSITIVE_EVIDENCE"):
        create_bybit_demo_operator_approval(
            unsafe,
            bars,
            approved_at=approved_at,
            confirmation_phrase="APPROVE_BYBIT_DEMO_EXECUTION",
        )


def test_dry_selector_must_independently_choose_the_approved_signal() -> None:
    row, approved_bars = _source_row()
    decision = datetime.fromisoformat(row["decision_time"])
    approved_at = decision + timedelta(minutes=6)
    approval = create_bybit_demo_operator_approval(
        row,
        approved_bars,
        approved_at=approved_at,
        confirmation_phrase="APPROVE_BYBIT_DEMO_EXECUTION",
    )
    bars_by_symbol = {
        "C00USDT": approved_bars,
        "C01USDT": _bars("C01USDT", trending=False),
    }
    selection = dry_check_approved_opportunity_matches_demo_selector(
        approval,
        row,
        bars_by_symbol,
        instruments={symbol: _instrument(symbol) for symbol in bars_by_symbol},
        strategy_config=CryptoPerpStrategyConfig(),
        session_state=_session(),
        now=approved_at,
    )
    assert selection.selected_trade_plan is not None
    assert selection.selected_trade_plan.symbol == approval.symbol
    assert selection.selected_trade_plan.side.value == approval.side
    assert selection.selected_trade_plan.decision_time == approval.decision_time

    changed = dict(row)
    changed["snapshot_id"] = "b" * 64
    with pytest.raises(ValueError, match="snapshot_id"):
        dry_check_approved_opportunity_matches_demo_selector(
            approval,
            changed,
            bars_by_symbol,
            instruments={symbol: _instrument(symbol) for symbol in bars_by_symbol},
            strategy_config=CryptoPerpStrategyConfig(),
            session_state=_session(),
            now=approved_at,
        )


def test_client_guard_allows_one_exact_entry_and_same_trade_protection_close() -> None:
    row, bars = _source_row()
    decision = datetime.fromisoformat(row["decision_time"])
    approved_at = decision + timedelta(minutes=6)
    approval = create_bybit_demo_operator_approval(
        row,
        bars,
        approved_at=approved_at,
        confirmation_phrase="APPROVE_BYBIT_DEMO_EXECUTION",
    )
    raw = _FakeDemoClient()
    client = OperatorApprovedBybitDemoClient(raw, approval, now=approved_at)
    entry_side = "Buy" if approval.side == "LONG" else "Sell"
    close_side = "Sell" if approval.side == "LONG" else "Buy"
    entry = BybitDemoOrderRequest(
        symbol=approval.symbol,
        side=entry_side,
        quantity=approval.maximum_entry_quantity,
        order_link_id=approval.expected_entry_order_link_id,
    )
    client.place_market_order(entry)
    assert client.entry_approval_consumed is True
    assert len(raw.orders) == 1

    with pytest.raises(ValueError, match="already been consumed"):
        client.place_market_order(entry)

    protection = BybitDemoProtectionRequest(
        symbol=approval.symbol,
        side=entry_side,
        average_entry_price=Decimal("100"),
        take_profit_price=Decimal("102") if entry_side == "Buy" else Decimal("98"),
        stop_loss_price=Decimal("98") if entry_side == "Buy" else Decimal("102"),
    )
    client.set_full_position_protection(protection)
    assert len(raw.protections) == 1

    close = BybitDemoOrderRequest(
        symbol=approval.symbol,
        side=close_side,
        quantity=approval.maximum_entry_quantity,
        order_link_id=approval.expected_close_order_link_id,
        reduce_only=True,
    )
    client.place_market_order(close)
    assert len(raw.orders) == 2

    wrong = BybitDemoOrderRequest(
        symbol="C01USDT",
        side=entry_side,
        quantity=Decimal("0.001"),
        order_link_id=approval.expected_entry_order_link_id,
    )
    fresh_client = OperatorApprovedBybitDemoClient(raw, approval, now=approved_at)
    with pytest.raises(ValueError, match="another symbol"):
        fresh_client.place_market_order(wrong)
