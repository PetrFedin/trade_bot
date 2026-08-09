from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.application.composition import ProductConfig, build_local_product
from app.domain.trading import Bar, Fill, Side
from app.execution.trade_fills import ExplicitZeroPaperFeeModel
from app.observability.readiness import OperationalSnapshot
from app.oms.store import OrderState
from app.risk.pretrade import RiskLimits

NOW = datetime(2026, 8, 7, 20, 0, tzinfo=UTC)


def config() -> ProductConfig:
    return ProductConfig(
        opening_cash=Decimal("10000"),
        target_quantity=Decimal("1"),
        risk_limits=RiskLimits(
            maximum_order_notional=Decimal("1000"),
            maximum_symbol_notional=Decimal("2000"),
            maximum_gross_notional=Decimal("5000"),
        ),
    )


def bars() -> list[Bar]:
    return [
        Bar("AAPL", NOW - timedelta(minutes=2), Decimal("100")),
        Bar("AAPL", NOW - timedelta(minutes=1), Decimal("101")),
        Bar("AAPL", NOW, Decimal("102")),
    ]


def test_local_composition_wires_one_coherent_product_graph(tmp_path) -> None:
    runtime = build_local_product(config=config(), state_directory=tmp_path)
    assert runtime.paper_pipeline.risk is runtime.risk_engine
    assert runtime.paper_pipeline.risk_admission is runtime.risk_admission
    assert runtime.paper_pipeline.ledger is runtime.portfolio
    assert runtime.order_lifecycle.store is runtime.oms_store
    assert runtime.reconciler.store is runtime.oms_store

    _, intent, decision = runtime.paper_pipeline.plan(bars())
    assert intent is not None and decision is not None and decision.approved
    assert runtime.paper_pipeline.last_recorded_risk is not None
    assert len(runtime.risk_admission.journal.verify()) == 1

    prepared = runtime.order_lifecycle.prepare(intent, decision, occurred_at=NOW)
    assert prepared.record.state is OrderState.OUTBOXED
    assert len(runtime.oms_store.pending_outbox()) == 1
    assert runtime.oms_store.get_by_client_order_id(prepared.client_order_id) == prepared.record


def test_local_composition_replays_portfolio_into_runtime_after_restart(tmp_path) -> None:
    runtime = build_local_product(config=config(), state_directory=tmp_path)
    fill = Fill(
        fill_id="composition-fill",
        order_intent_id="composition-intent",
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("1"),
        price=Decimal("100"),
        occurred_at=NOW,
        fee=Decimal("1"),
    )
    assert runtime.portfolio_store.append_fill(fill)
    assert runtime.portfolio.position("AAPL").quantity == 0

    restarted = build_local_product(config=config(), state_directory=tmp_path)
    assert restarted.portfolio.cash == Decimal("9899")
    assert restarted.portfolio.position("AAPL").quantity == Decimal("1")
    assert restarted.portfolio.position("AAPL").average_cost == Decimal("101")
    assert restarted.paper_pipeline.ledger is restarted.portfolio


def test_fill_accounting_requires_explicit_fee_provider(tmp_path) -> None:
    runtime = build_local_product(config=config(), state_directory=tmp_path)
    with pytest.raises(RuntimeError, match="fee provider is not configured"):
        runtime.require_fill_accounting()

    configured = build_local_product(
        config=config(),
        state_directory=tmp_path,
        fee_provider=ExplicitZeroPaperFeeModel(),
    )
    assert configured.require_fill_accounting().portfolio is configured.portfolio_store


def test_local_composition_operational_gate_remains_paper_only(tmp_path) -> None:
    runtime = build_local_product(config=config(), state_directory=tmp_path)
    result = runtime.operational_readiness.evaluate(
        OperationalSnapshot(
            market_data_age_seconds=Decimal("1"),
            stream_silence_seconds=Decimal("1"),
            broker_latency_ms=Decimal("10"),
            broker_error_fraction=Decimal("0"),
            uncertain_orders=0,
            reconciliation_age_seconds=Decimal("1"),
            cash_mismatch=Decimal("0"),
            position_mismatches=0,
            daily_pnl=Decimal("0"),
            drawdown=Decimal("0"),
            kill_switch_engaged=False,
            market_data_ready=True,
            stream_ready=True,
            broker_connected=True,
            portfolio_reconciled=True,
        )
    )
    assert result.ready_for_paper_operation
    assert not result.live_trading_allowed
