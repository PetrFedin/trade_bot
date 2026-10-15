from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.application.composition import ProductConfig, build_local_product
from app.application.paper_cycle import PaperCycleService
from app.domain.trading import Bar
from app.execution.trade_fills import ExplicitZeroPaperFeeModel
from app.marketdata.continuity import (
    OperationalContinuityCheckpoint,
    continuity_checkpoint_id,
)
from app.marketdata.operational import OperationalBar
from app.observability.authority import (
    OperationalMarketScope,
    RuntimeTelemetry,
    SessionRiskTruth,
)
from app.oms.reconciliation import BrokerPortfolioTruth
from app.oms.store import OrderState
from app.risk.pretrade import OperationalRiskContext, RiskLimits
from app.runtime.alpaca_paper_adapter_v100 import (
    AlpacaPaperCredentialsV100,
    AlpacaTradeUpdateStreamV100,
)
from app.runtime.paper_broker_contract_v99 import BrokerOrder, BrokerOrderStatus
from app.runtime.paper_dispatch_control import DispatchBlocked, DispatchControlMode
from tests.financial_activity_truth_support import ready_financial_activity_truth

NOW = datetime(2026, 9, 12, 16, 0, tzinfo=UTC)
MARKET_SCOPE = OperationalMarketScope(
    provider="ALPACA",
    venue="PAPER",
    symbol="AAPL",
    interval_seconds=60,
)


def config() -> ProductConfig:
    return ProductConfig(
        opening_cash=Decimal("1000"),
        target_quantity=Decimal("1"),
        risk_limits=RiskLimits(
            maximum_order_notional=Decimal("200"),
            maximum_symbol_notional=Decimal("200"),
            maximum_gross_notional=Decimal("200"),
        ),
    )


def bars() -> list[Bar]:
    return [
        Bar("AAPL", NOW - timedelta(minutes=2), Decimal("100")),
        Bar("AAPL", NOW - timedelta(minutes=1), Decimal("101")),
        Bar("AAPL", NOW, Decimal("102")),
    ]


def risk_context(runtime) -> OperationalRiskContext:
    marks = {"AAPL": Decimal("102")}
    return OperationalRiskContext(
        price_timestamp=NOW,
        decision_time=NOW,
        market_open=True,
        halted=False,
        spread_bps=Decimal("1"),
        estimated_slippage_bps=Decimal("1"),
        daily_pnl=Decimal("0"),
        drawdown=Decimal("0"),
        turnover_notional=Decimal("0"),
        average_daily_dollar_volume=Decimal("1000000"),
        portfolio_equity=runtime.portfolio.equity(marks),
        sector_notional=Decimal("0"),
        annualized_volatility=Decimal("0.20"),
        available_cash=runtime.portfolio.cash,
        portfolio_mark_prices=marks,
    )


def listening_stream() -> AlpacaTradeUpdateStreamV100:
    credentials = AlpacaPaperCredentialsV100(key_id="paper-key", secret_key="paper-secret")
    stream = AlpacaTradeUpdateStreamV100(generation=1, credentials=credentials)
    stream.authentication_frame()
    stream.ingest(
        json.dumps({"stream": "authorization", "data": {"status": "authorized"}}),
        received_at=NOW,
        expected_generation=1,
    )
    stream.ingest(
        json.dumps({"stream": "listening", "data": {"streams": ["trade_updates"]}}),
        received_at=NOW,
        expected_generation=1,
    )
    return stream


class MutableSessionRisk:
    def __init__(self, *, kill_switch_engaged: bool = False) -> None:
        self.kill_switch_engaged = kill_switch_engaged
        self.calls = 0

    def __call__(self) -> SessionRiskTruth:
        self.calls += 1
        return SessionRiskTruth(
            daily_pnl=Decimal("0"),
            drawdown=Decimal("0"),
            kill_switch_engaged=self.kill_switch_engaged,
        )


def ready_runtime_telemetry() -> RuntimeTelemetry:
    return RuntimeTelemetry(
        stream_ready=True,
        stream_last_message_at=NOW,
        broker_connected=True,
        broker_latency_ms=Decimal("1"),
        broker_error_fraction=Decimal("0"),
    )


def seed_market_authority(runtime) -> None:
    bar = OperationalBar(
        provider=MARKET_SCOPE.provider,
        venue=MARKET_SCOPE.venue,
        symbol=MARKET_SCOPE.symbol,
        interval_seconds=MARKET_SCOPE.interval_seconds,
        open_time=NOW - timedelta(seconds=MARKET_SCOPE.interval_seconds),
        close_time=NOW,
        source_timestamp=NOW - timedelta(seconds=1),
        received_at=NOW,
        source_event_id="final-dispatch-authority-bar",
        is_final=True,
        open=Decimal("100"),
        high=Decimal("103"),
        low=Decimal("99"),
        close=Decimal("102"),
        volume=Decimal("10"),
    )
    runtime.operational_marketdata.record_finalized_for_strategy(
        bar,
        strategy_id="final-dispatch-authority",
        recorded_at=NOW,
    )
    checkpoint = OperationalContinuityCheckpoint(
        checkpoint_id=continuity_checkpoint_id(
            previous_checkpoint_id=None,
            provider=bar.provider,
            venue=bar.venue,
            symbol=bar.symbol,
            interval_seconds=bar.interval_seconds,
            through_bar_id=bar.bar_id,
            through_close_time=bar.close_time,
            evidence_source="final-dispatch-authority",
        ),
        previous_checkpoint_id=None,
        provider=bar.provider,
        venue=bar.venue,
        symbol=bar.symbol,
        interval_seconds=bar.interval_seconds,
        through_bar_id=bar.bar_id,
        through_close_time=bar.close_time,
        established_at=NOW,
        evidence_source="final-dispatch-authority",
    )
    runtime.marketdata_continuity.append(checkpoint)


class CountingBroker:
    paper_order_writes_enabled = True

    def __init__(self) -> None:
        self.submit_calls = 0
        self.get_calls = 0
        self.orders: dict[str, BrokerOrder] = {}

    def submit_limit_order(self, **kwargs) -> BrokerOrder:
        self.submit_calls += 1
        order = BrokerOrder(
            client_order_id=kwargs["client_order_id"],
            broker_order_id=f"dispatch-broker-{self.submit_calls}",
            instrument=kwargs["instrument"],
            side=kwargs["side"],
            quantity=kwargs["quantity"],
            limit_price=kwargs["limit_price"],
            status=BrokerOrderStatus.ACKNOWLEDGED,
            filled_quantity=Decimal("0"),
            updated_at=NOW,
        )
        self.orders[order.client_order_id] = order
        return order

    def get_order_by_client_order_id(self, client_order_id: str):
        self.get_calls += 1
        return self.orders.get(client_order_id)


def build_cycle(
    tmp_path,
    broker: CountingBroker,
    *,
    with_authority: bool = True,
    session_risk: MutableSessionRisk | None = None,
):
    runtime = build_local_product(
        config=config(),
        state_directory=tmp_path,
        fee_provider=ExplicitZeroPaperFeeModel(),
    )
    resolved_session_risk = (
        MutableSessionRisk() if session_risk is None else session_risk
    )
    if with_authority:
        seed_market_authority(runtime)
    cycle = PaperCycleService(
        runtime=runtime,
        broker=broker,
        trade_stream=listening_stream(),
        stream_generation=1,
        financial_activity_truth=ready_financial_activity_truth(tmp_path, now=NOW),
        operational_market_scope=MARKET_SCOPE if with_authority else None,
        runtime_telemetry=ready_runtime_telemetry if with_authority else None,
        session_risk=resolved_session_risk if with_authority else None,
    )
    if with_authority:
        reconciliation = cycle.reconcile_portfolio(
            BrokerPortfolioTruth(cash=runtime.portfolio.cash, positions=()),
            occurred_at=NOW,
        )
        assert reconciliation.matched
    return runtime, cycle, resolved_session_risk


def test_readiness_failure_after_outbox_durably_halts_before_broker_submit(tmp_path) -> None:
    broker = CountingBroker()
    session_risk = MutableSessionRisk()
    runtime, cycle, _ = build_cycle(tmp_path, broker, session_risk=session_risk)
    runtime.dispatch_control.arm(
        operator_id="operator-f05",
        reason="qualification arm",
        occurred_at=NOW,
    )
    planning = cycle.plan_and_prepare(
        bars(), decision_time=NOW, risk_context=risk_context(runtime)
    )
    assert planning.prepared is not None
    assert planning.prepared.record.state is OrderState.OUTBOXED
    assert len(runtime.oms_store.pending_outbox()) == 1

    session_risk.kill_switch_engaged = True
    with pytest.raises(DispatchBlocked) as blocked:
        cycle.execute_next_submit(occurred_at=NOW + timedelta(seconds=1))
    assert blocked.value.reasons == ("KILL_SWITCH_ENGAGED",)
    assert broker.submit_calls == 0
    assert broker.get_calls == 0
    assert runtime.oms_store.get(planning.intent.intent_id).state is OrderState.OUTBOXED
    assert len(runtime.oms_store.pending_outbox()) == 1
    assert runtime.dispatch_control.current().mode is DispatchControlMode.HALTED
    event_types = [str(event["event_type"]) for event in runtime.dispatch_control.events()]
    assert event_types == ["ARM", "HALT"]
    assert session_risk.calls == 1

    restarted_session_risk = MutableSessionRisk()
    restarted_runtime, restarted_cycle, _ = build_cycle(
        tmp_path,
        broker,
        session_risk=restarted_session_risk,
    )
    assert restarted_runtime.dispatch_control.current().mode is DispatchControlMode.HALTED
    with pytest.raises(DispatchBlocked) as still_blocked:
        restarted_cycle.execute_next_submit(occurred_at=NOW + timedelta(seconds=2))
    assert still_blocked.value.reasons == ("DISPATCH_CONTROL_HALTED",)
    assert broker.submit_calls == 0
    assert len(restarted_runtime.oms_store.pending_outbox()) == 1

    restarted_runtime.dispatch_control.arm(
        operator_id="operator-f05",
        reason="explicit re-arm after investigation",
        occurred_at=NOW + timedelta(seconds=3),
    )
    execution = restarted_cycle.execute_next_submit(occurred_at=NOW + timedelta(seconds=4))
    assert execution is not None and execution.mutation_attempted
    assert execution.record.state is OrderState.ACKNOWLEDGED
    assert broker.submit_calls == 1
    assert restarted_runtime.oms_store.pending_outbox() == ()
    assert restarted_session_risk.calls == 2
    assert [
        str(event["event_type"]) for event in restarted_runtime.dispatch_control.events()
    ][-2:] == ["ARM", "DISPATCH_AUTHORIZED"]


def test_missing_operational_snapshot_fails_closed_and_persists_halt(tmp_path) -> None:
    broker = CountingBroker()
    runtime, cycle, _ = build_cycle(tmp_path, broker, with_authority=False)
    runtime.dispatch_control.arm(
        operator_id="operator-f05",
        reason="qualification arm",
        occurred_at=NOW,
    )
    planning = cycle.plan_and_prepare(
        bars(), decision_time=NOW, risk_context=risk_context(runtime)
    )
    assert planning.prepared is not None

    with pytest.raises(DispatchBlocked) as blocked:
        cycle.execute_next_submit(occurred_at=NOW + timedelta(seconds=1))
    assert blocked.value.reasons == ("OPERATIONAL_SNAPSHOT_REQUIRED",)
    assert broker.submit_calls == 0
    assert runtime.dispatch_control.current().mode is DispatchControlMode.HALTED
    assert runtime.oms_store.get(planning.intent.intent_id).state is OrderState.OUTBOXED


def test_expired_arm_cannot_authorize_outbox(tmp_path) -> None:
    broker = CountingBroker()
    runtime, cycle, _ = build_cycle(tmp_path, broker)
    runtime.dispatch_control.arm(
        operator_id="operator-f05",
        reason="short qualification arm",
        occurred_at=NOW,
        ttl=timedelta(seconds=1),
    )
    planning = cycle.plan_and_prepare(
        bars(), decision_time=NOW, risk_context=risk_context(runtime)
    )
    assert planning.prepared is not None

    with pytest.raises(DispatchBlocked) as blocked:
        cycle.execute_next_submit(occurred_at=NOW + timedelta(seconds=2))
    assert blocked.value.reasons == ("DISPATCH_CONTROL_EXPIRED",)
    assert broker.submit_calls == 0
    assert runtime.oms_store.get(planning.intent.intent_id).state is OrderState.OUTBOXED
    assert len(runtime.oms_store.pending_outbox()) == 1
