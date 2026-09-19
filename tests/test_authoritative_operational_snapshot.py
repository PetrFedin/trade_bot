from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.application.order_lifecycle import PaperOrderLifecycle
from app.domain.trading import OrderIntent, Side
from app.marketdata.continuity import (
    OperationalContinuityCheckpoint,
    continuity_checkpoint_id,
)
from app.marketdata.operational import OperationalBar
from app.observability.authority import (
    AuthoritativeOperationalSnapshotAssembler,
    OperationalMarketScope,
    RuntimeTelemetry,
    SessionRiskTruth,
)
from app.observability.readiness import OperationalReadinessEvaluator
from app.oms.portfolio_reconciliation import PortfolioReconciliationEvidence
from app.oms.store import DurableOmsStore, OrderState
from app.risk.pretrade import (
    PreTradeRiskEngine,
    RiskEvaluationMode,
    RiskLimits,
)

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
SCOPE = OperationalMarketScope(
    provider="ALPACA",
    venue="PAPER",
    symbol="AAPL",
    interval_seconds=60,
)


def operational_bar() -> OperationalBar:
    close_time = NOW - timedelta(seconds=1)
    return OperationalBar(
        provider=SCOPE.provider,
        venue=SCOPE.venue,
        symbol=SCOPE.symbol,
        interval_seconds=SCOPE.interval_seconds,
        open_time=close_time - timedelta(seconds=60),
        close_time=close_time,
        source_timestamp=close_time,
        received_at=close_time + timedelta(milliseconds=100),
        source_event_id="bar-1",
        is_final=True,
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("101"),
        volume=Decimal("10"),
    )


def continuity_for(bar: OperationalBar) -> OperationalContinuityCheckpoint:
    return OperationalContinuityCheckpoint(
        checkpoint_id=continuity_checkpoint_id(
            previous_checkpoint_id=None,
            provider=bar.provider,
            venue=bar.venue,
            symbol=bar.symbol,
            interval_seconds=bar.interval_seconds,
            through_bar_id=bar.bar_id,
            through_close_time=bar.close_time,
            evidence_source="test-authority",
        ),
        previous_checkpoint_id=None,
        provider=bar.provider,
        venue=bar.venue,
        symbol=bar.symbol,
        interval_seconds=bar.interval_seconds,
        through_bar_id=bar.bar_id,
        through_close_time=bar.close_time,
        established_at=NOW,
        evidence_source="test-authority",
    )


def matched_reconciliation() -> PortfolioReconciliationEvidence:
    return PortfolioReconciliationEvidence(
        reconciliation_id="reconciliation-1",
        internal_cash=Decimal("1000"),
        broker_cash=Decimal("1000"),
        internal_positions=(),
        broker_positions=(),
        cash_delta=Decimal("0"),
        position_deltas=(),
        reasons=(),
        cash_tolerance=Decimal("0.01"),
        quantity_tolerance=Decimal("0"),
        occurred_at=NOW - timedelta(seconds=3),
    )


class MarketAuthority:
    def __init__(self, bar: OperationalBar, *, conflicts: int = 0) -> None:
        self.bar = bar
        self.conflicts = conflicts

    def recent_bars(self, **kwargs):
        assert kwargs["provider"] == SCOPE.provider
        assert kwargs["venue"] == SCOPE.venue
        assert kwargs["symbol"] == SCOPE.symbol
        assert kwargs["interval_seconds"] == SCOPE.interval_seconds
        assert kwargs["through_close_time"] == self.bar.close_time
        assert kwargs["limit"] == 1
        return (self.bar,)

    def conflict_count(self) -> int:
        return self.conflicts


class ContinuityAuthority:
    def __init__(self, checkpoint: OperationalContinuityCheckpoint | None) -> None:
        self.checkpoint = checkpoint

    def latest(self, **kwargs):
        assert kwargs == {
            "provider": SCOPE.provider,
            "venue": SCOPE.venue,
            "symbol": SCOPE.symbol,
            "interval_seconds": SCOPE.interval_seconds,
        }
        return self.checkpoint


class OmsAuthority:
    def __init__(self, count: int = 0) -> None:
        self.count = count

    def operational_blocking_count(self) -> int:
        return self.count


class CountAuthority:
    def __init__(self, count: int = 0) -> None:
        self.count = count

    def unresolved_count(self) -> int:
        return self.count


class ReconciliationAuthority:
    def __init__(self, evidence: PortfolioReconciliationEvidence | None) -> None:
        self.evidence = evidence

    def latest(self) -> PortfolioReconciliationEvidence | None:
        return self.evidence


def assembler(
    *,
    market_conflicts: int = 0,
    oms_blockers: int = 0,
    unresolved_facts: int = 0,
    unresolved_checkpoints: int = 0,
    reconciliation: PortfolioReconciliationEvidence | None = None,
    runtime_telemetry=True,
    session_risk=True,
) -> AuthoritativeOperationalSnapshotAssembler:
    bar = operational_bar()
    return AuthoritativeOperationalSnapshotAssembler(
        market_scope=SCOPE,
        marketdata=MarketAuthority(bar, conflicts=market_conflicts),
        continuity=ContinuityAuthority(continuity_for(bar)),
        oms=OmsAuthority(oms_blockers),
        reconciliation=ReconciliationAuthority(
            matched_reconciliation() if reconciliation is None else reconciliation
        ),
        execution_facts=CountAuthority(unresolved_facts),
        execution_checkpoints=CountAuthority(unresolved_checkpoints),
        runtime_telemetry=(
            (
                lambda: RuntimeTelemetry(
                    stream_ready=True,
                    stream_last_message_at=NOW - timedelta(seconds=2),
                    broker_connected=True,
                    broker_latency_ms=Decimal("10"),
                    broker_error_fraction=Decimal("0"),
                )
            )
            if runtime_telemetry
            else None
        ),
        session_risk=(
            (
                lambda: SessionRiskTruth(
                    daily_pnl=Decimal("0"),
                    drawdown=Decimal("0"),
                    kill_switch_engaged=False,
                )
            )
            if session_risk
            else None
        ),
        clock=lambda: NOW,
    )


def test_authoritative_assembler_builds_ready_snapshot_from_factual_sources() -> None:
    snapshot = assembler()()

    assert snapshot.market_data_age_seconds == Decimal("1.0")
    assert snapshot.stream_silence_seconds == Decimal("2.0")
    assert snapshot.reconciliation_age_seconds == Decimal("3.0")
    assert snapshot.uncertain_orders == 0
    assert snapshot.unresolved_execution_facts == 0
    assert snapshot.unresolved_execution_checkpoints == 0
    assert snapshot.market_data_conflicts == 0
    assert snapshot.authority_reasons == ()
    assert snapshot.market_data_ready
    assert snapshot.stream_ready
    assert snapshot.broker_connected
    assert snapshot.portfolio_reconciled

    readiness = OperationalReadinessEvaluator().evaluate(snapshot)
    assert readiness.ready_for_paper_operation
    assert readiness.reasons == ()


def test_missing_runtime_and_session_authority_fail_closed_without_optimistic_defaults() -> None:
    snapshot = assembler(runtime_telemetry=False, session_risk=False)()

    assert not snapshot.stream_ready
    assert not snapshot.broker_connected
    assert snapshot.kill_switch_engaged
    assert set(snapshot.authority_reasons) == {
        "RUNTIME_TELEMETRY_MISSING",
        "SESSION_RISK_AUTHORITY_MISSING",
    }

    readiness = OperationalReadinessEvaluator().evaluate(snapshot)
    assert not readiness.ready_for_paper_operation
    assert {
        "BROKER_DISCONNECTED",
        "KILL_SWITCH_ENGAGED",
        "RUNTIME_TELEMETRY_MISSING",
        "SESSION_RISK_AUTHORITY_MISSING",
        "TRADE_STREAM_NOT_READY",
    }.issubset(set(readiness.reasons))


def test_durable_conflicts_and_projection_gaps_are_hard_readiness_blockers() -> None:
    mismatch = PortfolioReconciliationEvidence(
        reconciliation_id="reconciliation-mismatch",
        internal_cash=Decimal("1000"),
        broker_cash=Decimal("990"),
        internal_positions=(),
        broker_positions=(),
        cash_delta=Decimal("-10"),
        position_deltas=(),
        reasons=("CASH_MISMATCH",),
        cash_tolerance=Decimal("0.01"),
        quantity_tolerance=Decimal("0"),
        occurred_at=NOW - timedelta(seconds=3),
    )
    snapshot = assembler(
        market_conflicts=1,
        oms_blockers=1,
        unresolved_facts=1,
        unresolved_checkpoints=1,
        reconciliation=mismatch,
    )()

    readiness = OperationalReadinessEvaluator().evaluate(snapshot)
    assert not readiness.ready_for_paper_operation
    assert {
        "CASH_MISMATCH",
        "EXECUTION_ACCOUNTING_NOT_CONVERGED",
        "MARKET_DATA_CONFLICT_PRESENT",
        "PORTFOLIO_NOT_RECONCILED",
        "UNCERTAIN_ORDERS_PRESENT",
    }.issubset(set(readiness.reasons))


def test_missing_market_continuity_is_explicitly_not_ready() -> None:
    bar = operational_bar()
    subject = AuthoritativeOperationalSnapshotAssembler(
        market_scope=SCOPE,
        marketdata=MarketAuthority(bar),
        continuity=ContinuityAuthority(None),
        oms=OmsAuthority(),
        reconciliation=ReconciliationAuthority(matched_reconciliation()),
        execution_facts=CountAuthority(),
        execution_checkpoints=CountAuthority(),
        runtime_telemetry=lambda: RuntimeTelemetry(
            stream_ready=True,
            stream_last_message_at=NOW,
            broker_connected=True,
            broker_latency_ms=Decimal("1"),
            broker_error_fraction=Decimal("0"),
        ),
        session_risk=lambda: SessionRiskTruth(
            daily_pnl=Decimal("0"),
            drawdown=Decimal("0"),
            kill_switch_engaged=False,
        ),
        clock=lambda: NOW,
    )

    snapshot = subject()
    assert not snapshot.market_data_ready
    assert snapshot.authority_reasons == ("MARKET_DATA_CONTINUITY_MISSING",)
    assert "MARKET_DATA_NOT_READY" in OperationalReadinessEvaluator().evaluate(snapshot).reasons


def intent() -> OrderIntent:
    return OrderIntent(
        intent_id="authority-order",
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("1"),
        limit_price=Decimal("100"),
        created_at=NOW,
        strategy_id="authority-test",
    )


def decision(value: OrderIntent):
    return PreTradeRiskEngine(
        RiskLimits(
            maximum_order_notional=Decimal("1000"),
            maximum_symbol_notional=Decimal("1000"),
            maximum_gross_notional=Decimal("1000"),
        )
    ).evaluate(
        value,
        current_symbol_notional=Decimal("0"),
        current_gross_notional=Decimal("0"),
        mode=RiskEvaluationMode.REPLAY,
    )


def test_sqlite_oms_operational_blocking_count_tracks_uncertain_reconciliation_manual(
    tmp_path,
) -> None:
    store = DurableOmsStore(tmp_path / "oms.sqlite")
    value = intent()
    PaperOrderLifecycle(store).prepare(value, decision(value), occurred_at=NOW)
    assert store.operational_blocking_count() == 0

    store.transition(
        value.intent_id,
        OrderState.SUBMIT_STARTED,
        event_id="submit-started",
        occurred_at=NOW,
    )
    store.transition(
        value.intent_id,
        OrderState.UNCERTAIN,
        event_id="submit-uncertain",
        occurred_at=NOW,
    )
    assert store.operational_blocking_count() == 1

    store.transition(
        value.intent_id,
        OrderState.RECONCILING,
        event_id="reconciling",
        occurred_at=NOW,
    )
    assert store.operational_blocking_count() == 1

    store.transition(
        value.intent_id,
        OrderState.MANUAL,
        event_id="manual",
        occurred_at=NOW,
    )
    assert store.operational_blocking_count() == 1
