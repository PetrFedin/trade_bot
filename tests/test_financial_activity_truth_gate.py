from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.application.composition import ProductConfig, build_local_product
from app.application.paper_cycle import PaperCycleService
from app.domain.trading import Bar, Fill, Side
from app.execution.financial_activity_gate import BoundFinancialActivityTruthProvider
from app.execution.financial_activity_store import (
    BrokerFinancialActivity,
    SQLiteFinancialActivityStore,
)
from app.execution.trade_fills import ExplicitZeroPaperFeeModel
from app.observability.readiness import OperationalSnapshot
from app.oms.store import OrderRecord, OrderState
from app.risk.pretrade import OperationalRiskContext, RiskLimits
from app.runtime.alpaca_paper_adapter_v100 import (
    AlpacaPaperCredentialsV100,
    AlpacaTradeUpdateStreamV100,
)
from app.runtime.paper_broker_contract_v99 import BrokerOrder, BrokerOrderStatus
from app.runtime.paper_dispatch_control import DispatchBlocked, DispatchControlMode

NOW = datetime(2026, 9, 15, 18, 0, tzinfo=UTC)
ACCOUNT = "paper-account:f21c"
RELEASE = "release:f21c"


def config() -> ProductConfig:
    return ProductConfig(
        opening_cash=Decimal("1000"),
        target_quantity=Decimal("1"),
        risk_limits=RiskLimits(
            maximum_order_notional=Decimal("1000"),
            maximum_symbol_notional=Decimal("2000"),
            maximum_gross_notional=Decimal("5000"),
            maximum_position_fraction_of_equity=Decimal("1"),
            maximum_sector_fraction_of_equity=Decimal("1"),
        ),
    )


def rising_bars() -> list[Bar]:
    return [
        Bar("AAPL", NOW - timedelta(minutes=2), Decimal("100")),
        Bar("AAPL", NOW - timedelta(minutes=1), Decimal("101")),
        Bar("AAPL", NOW, Decimal("102")),
    ]


def falling_bars() -> list[Bar]:
    return [
        Bar("AAPL", NOW - timedelta(minutes=2), Decimal("105")),
        Bar("AAPL", NOW - timedelta(minutes=1), Decimal("104")),
        Bar("AAPL", NOW, Decimal("100")),
    ]


def risk_context(runtime, *, price: str = "102") -> OperationalRiskContext:
    marks = {"AAPL": Decimal(price)}
    position = runtime.portfolio.position("AAPL")
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
        sector_notional=position.quantity * marks["AAPL"],
        annualized_volatility=Decimal("0.20"),
        available_cash=runtime.portfolio.cash,
        portfolio_mark_prices=marks,
    )


def store(tmp_path) -> SQLiteFinancialActivityStore:
    return SQLiteFinancialActivityStore(tmp_path / "financial-truth.sqlite")


def provider(
    activity_store: SQLiteFinancialActivityStore,
    *,
    release_identity: str = RELEASE,
    maximum_age: timedelta = timedelta(minutes=5),
) -> BoundFinancialActivityTruthProvider:
    return BoundFinancialActivityTruthProvider(
        store=activity_store,
        account_identity=ACCOUNT,
        release_identity=release_identity,
        maximum_age=maximum_age,
    )


def mark_recovered(
    activity_store: SQLiteFinancialActivityStore,
    *,
    recovered_through: datetime = NOW,
    release_identity: str = RELEASE,
) -> None:
    activity_store.advance_recovery(
        account_identity=ACCOUNT,
        release_identity=release_identity,
        recovered_through=recovered_through,
        occurred_at=NOW,
    )


def activity(activity_id: str, activity_type: str = "FEE") -> BrokerFinancialActivity:
    amount = "-1" if activity_type == "FEE" else "1"
    payload = {"id": activity_id, "activity_type": activity_type, "net_amount": amount}
    return BrokerFinancialActivity(
        activity_id=activity_id,
        activity_type=activity_type,
        net_amount=Decimal(amount),
        currency="USD",
        occurred_at=NOW - timedelta(seconds=1),
        account_identity=ACCOUNT,
        release_identity=RELEASE,
        source_cursor="ROOT",
        canonical_payload=json.dumps(payload, sort_keys=True, separators=(",", ":")),
    )


def assert_buy_blocked_before_risk(runtime, truth, expected_reason: str) -> None:
    with pytest.raises(RuntimeError, match=expected_reason):
        runtime.paper_pipeline.plan(
            rising_bars(),
            decision_time=NOW,
            risk_context=risk_context(runtime),
            financial_activity_truth=truth,
        )
    assert runtime.risk_admission.journal.verify() == ()
    assert runtime.oms_store.pending_outbox() == ()


def test_missing_stale_pending_quarantined_and_wrong_release_block_buy_before_risk(
    tmp_path,
) -> None:
    missing_runtime = build_local_product(
        config=config(), state_directory=tmp_path / "missing"
    )
    missing_store = store(tmp_path / "missing")
    assert_buy_blocked_before_risk(
        missing_runtime,
        provider(missing_store),
        "FINANCIAL_ACTIVITY_RECOVERY_REQUIRED",
    )

    stale_runtime = build_local_product(config=config(), state_directory=tmp_path / "stale")
    stale_store = store(tmp_path / "stale")
    mark_recovered(stale_store, recovered_through=NOW - timedelta(minutes=6))
    assert_buy_blocked_before_risk(
        stale_runtime,
        provider(stale_store),
        "FINANCIAL_ACTIVITY_RECOVERY_STALE",
    )

    pending_runtime = build_local_product(
        config=config(), state_directory=tmp_path / "pending"
    )
    pending_store = store(tmp_path / "pending")
    mark_recovered(pending_store)
    pending_store.ingest(activity("pending-fee"), ingested_at=NOW)
    assert_buy_blocked_before_risk(
        pending_runtime,
        provider(pending_store),
        "FINANCIAL_ACTIVITY_PROJECTION_PENDING",
    )

    quarantine_runtime = build_local_product(
        config=config(), state_directory=tmp_path / "quarantine"
    )
    quarantine_store = store(tmp_path / "quarantine")
    mark_recovered(quarantine_store)
    quarantine_store.ingest(activity("unknown", "JNLC"), ingested_at=NOW)
    quarantine_store.quarantine(
        ACCOUNT,
        "unknown",
        reason="UNSUPPORTED_FINANCIAL_ACTIVITY:JNLC",
        occurred_at=NOW,
    )
    assert_buy_blocked_before_risk(
        quarantine_runtime,
        provider(quarantine_store),
        "FINANCIAL_ACTIVITY_QUARANTINED",
    )

    release_runtime = build_local_product(
        config=config(), state_directory=tmp_path / "release"
    )
    release_store = store(tmp_path / "release")
    mark_recovered(release_store, release_identity=RELEASE)
    assert_buy_blocked_before_risk(
        release_runtime,
        provider(release_store, release_identity="release:other"),
        "FINANCIAL_ACTIVITY_RECOVERY_REQUIRED",
    )


def test_clear_financial_truth_allows_buy_and_survives_store_reopen(tmp_path) -> None:
    runtime = build_local_product(config=config(), state_directory=tmp_path)
    activity_store = store(tmp_path)
    mark_recovered(activity_store)
    _, intent, decision = runtime.paper_pipeline.plan(
        rising_bars(),
        decision_time=NOW,
        risk_context=risk_context(runtime),
        financial_activity_truth=provider(activity_store),
    )
    assert intent is not None and intent.side is Side.BUY
    assert decision is not None and decision.approved

    reopened = SQLiteFinancialActivityStore(tmp_path / "financial-truth.sqlite")
    readiness = provider(reopened).readiness(now=NOW + timedelta(seconds=1))
    assert readiness.ready
    assert readiness.pending == 0
    assert readiness.quarantined == 0


class ExplodingTruth:
    def readiness(self, *, now: datetime):
        raise RuntimeError("financial store unavailable")


def test_sell_planning_does_not_read_broken_financial_truth(tmp_path) -> None:
    runtime = build_local_product(config=config(), state_directory=tmp_path)
    runtime.portfolio.apply_fill(
        Fill(
            fill_id="seed-f21c",
            order_intent_id="seed-f21c-intent",
            symbol="AAPL",
            side=Side.BUY,
            quantity=Decimal("1"),
            price=Decimal("100"),
            occurred_at=NOW - timedelta(minutes=5),
        )
    )
    _, intent, decision = runtime.paper_pipeline.plan(
        falling_bars(),
        decision_time=NOW,
        risk_context=risk_context(runtime, price="100"),
        financial_activity_truth=ExplodingTruth(),
    )
    assert intent is not None and intent.side is Side.SELL
    assert decision is not None and decision.approved


def ready_snapshot() -> OperationalSnapshot:
    return OperationalSnapshot(
        market_data_age_seconds=Decimal("0"),
        stream_silence_seconds=Decimal("0"),
        broker_latency_ms=Decimal("1"),
        broker_error_fraction=Decimal("0"),
        uncertain_orders=0,
        reconciliation_age_seconds=Decimal("0"),
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


class CountingBroker:
    paper_order_writes_enabled = True

    def __init__(self) -> None:
        self.submit_calls = 0
        self.orders: dict[str, BrokerOrder] = {}

    def submit_limit_order(self, **kwargs) -> BrokerOrder:
        self.submit_calls += 1
        order = BrokerOrder(
            client_order_id=kwargs["client_order_id"],
            broker_order_id="broker-f21c",
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

    def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        return self.orders.get(client_order_id)


def sell_record() -> OrderRecord:
    return OrderRecord(
        intent_id="f21c-sell-exit",
        client_order_id="f21c-sell-client",
        broker_order_id="",
        symbol="AAPL",
        side=Side.SELL,
        quantity=Decimal("1"),
        limit_price=Decimal("100"),
        filled_quantity=Decimal("0"),
        state=OrderState.OUTBOXED,
        version=1,
        updated_at=NOW,
    )


def test_pending_truth_after_planning_blocks_submit_without_halting_sell_authority(
    tmp_path,
) -> None:
    runtime = build_local_product(
        config=config(),
        state_directory=tmp_path,
        fee_provider=ExplicitZeroPaperFeeModel(),
    )
    activity_store = store(tmp_path)
    mark_recovered(activity_store)
    truth = provider(activity_store)
    broker = CountingBroker()
    runtime.dispatch_control.arm(
        operator_id="f21c-test",
        reason="financial truth dispatch qualification",
        occurred_at=NOW,
    )
    cycle = PaperCycleService(
        runtime=runtime,
        broker=broker,
        trade_stream=listening_stream(),
        stream_generation=1,
        financial_activity_truth=truth,
        operational_snapshot_provider=ready_snapshot,
    )

    planning = cycle.plan_and_prepare(
        rising_bars(),
        decision_time=NOW,
        risk_context=risk_context(runtime),
    )
    assert planning.prepared is not None
    assert planning.prepared.record.state is OrderState.OUTBOXED

    activity_store.ingest(activity("late-fee"), ingested_at=NOW + timedelta(seconds=1))
    with pytest.raises(DispatchBlocked) as blocked:
        cycle.execute_next_submit(occurred_at=NOW + timedelta(seconds=1))
    assert blocked.value.reasons == (
        "BROKER_FINANCIAL_ACTIVITY_NOT_READY",
        "FINANCIAL_ACTIVITY_PROJECTION_PENDING",
    )
    assert broker.submit_calls == 0
    assert runtime.oms_store.get(planning.intent.intent_id).state is OrderState.OUTBOXED
    assert runtime.dispatch_control.current().mode is DispatchControlMode.ARMED

    sell_authorization = cycle.final_dispatch.authorize(
        sell_record(),
        occurred_at=NOW + timedelta(seconds=1),
    )
    assert sell_authorization.intent_id == "f21c-sell-exit"
    assert runtime.dispatch_control.current().mode is DispatchControlMode.ARMED
