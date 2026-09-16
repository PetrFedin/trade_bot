from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.application.decision_worker import (
    OperationalDecisionWorker,
    OperationalDecisionWorkerPolicy,
)
from app.application.paper_cycle import PaperPlanningResult
from app.domain.trading import TargetPosition
from app.marketdata.continuity import (
    OperationalContinuityCheckpoint,
    SQLiteOperationalContinuityStore,
    SQLiteOperationalRepairBarStore,
    continuity_checkpoint_id,
)
from app.marketdata.decision_leases import (
    DecisionLeasePolicy,
    SQLiteDecisionLeaseStore,
    StaleDecisionLease,
)
from app.marketdata.operational import (
    OperationalBar,
    OperationalBarConflict,
    SQLiteOperationalMarketDataStore,
)
from app.observability.readiness import OperationalReadinessEvaluator, OperationalSnapshot
from app.risk.pretrade import OperationalRiskContext
from app.runtime.paper_dispatch_control import (
    DispatchControlMode,
    SQLitePaperDispatchControlStore,
)

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
BASE = NOW - timedelta(minutes=15)
STRATEGY = "paper-momentum-v1"


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


def bar(index: int, *, close: str | None = None) -> OperationalBar:
    open_time = BASE + timedelta(minutes=5 * index)
    close_time = open_time + timedelta(minutes=5)
    price = Decimal(close if close is not None else str(100 + index))
    return OperationalBar(
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
        open_time=open_time,
        close_time=close_time,
        source_timestamp=close_time - timedelta(milliseconds=1),
        received_at=close_time + timedelta(seconds=1),
        source_event_id=f"kline.5.BTCUSDT:{index}",
        is_final=True,
        open=price,
        high=price + Decimal("1"),
        low=price - Decimal("1"),
        close=price,
        volume=Decimal("10"),
        revision=0,
    )


def ready_snapshot(*, market_data_ready: bool = True) -> OperationalSnapshot:
    return OperationalSnapshot(
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
        market_data_ready=market_data_ready,
        stream_ready=True,
        broker_connected=True,
        portfolio_reconciled=True,
    )


class FakeRiskContextProvider:
    def context_for_decision(self, *, receipt, bars, decision_time):
        return OperationalRiskContext(
            price_timestamp=bars[-1].timestamp,
            decision_time=decision_time,
            market_open=True,
            halted=False,
            spread_bps=Decimal("1"),
            estimated_slippage_bps=Decimal("1"),
            daily_pnl=Decimal("0"),
            drawdown=Decimal("0"),
            turnover_notional=Decimal("0"),
            average_daily_dollar_volume=Decimal("1000000"),
            portfolio_equity=Decimal("1000"),
            sector_notional=Decimal("0"),
            annualized_volatility=Decimal("0.2"),
            available_cash=Decimal("1000"),
            portfolio_mark_prices={receipt.symbol: bars[-1].close},
        )


class FakePlanner:
    def __init__(self) -> None:
        self.calls = 0

    def plan_and_prepare(
        self,
        bars,
        *,
        decision_time,
        risk_context,
        kill_switch_engaged=False,
    ) -> PaperPlanningResult:
        self.calls += 1
        assert risk_context.decision_time == decision_time
        assert not kill_switch_engaged
        target = TargetPosition(
            symbol=bars[-1].symbol,
            quantity=Decimal("0"),
            reference_price=bars[-1].close,
            generated_at=bars[-1].timestamp,
            strategy_id=STRATEGY,
        )
        target.validate()
        return PaperPlanningResult(target, None, None, None)


class SlowPlanner(FakePlanner):
    def __init__(self, clock: MutableClock, delay: timedelta) -> None:
        super().__init__()
        self.clock = clock
        self.delay = delay

    def plan_and_prepare(self, bars, **kwargs) -> PaperPlanningResult:
        result = super().plan_and_prepare(bars, **kwargs)
        self.clock.advance(self.delay)
        return result


class ConflictDuringPlanner(FakePlanner):
    def __init__(self, marketdata: SQLiteOperationalMarketDataStore) -> None:
        super().__init__()
        self.marketdata = marketdata

    def plan_and_prepare(self, bars, **kwargs) -> PaperPlanningResult:
        result = super().plan_and_prepare(bars, **kwargs)
        conflicting = bar(1, close="777")
        with pytest.raises(OperationalBarConflict):
            self.marketdata.record_finalized_for_strategy(
                conflicting,
                strategy_id=STRATEGY,
                recorded_at=NOW + timedelta(seconds=3),
            )
        return result


def build_stack(tmp_path, *, with_checkpoint: bool = True):
    marketdata_path = tmp_path / "marketdata.sqlite"
    marketdata = SQLiteOperationalMarketDataStore(marketdata_path)
    repair = SQLiteOperationalRepairBarStore(marketdata_path)
    continuity = SQLiteOperationalContinuityStore(marketdata_path)
    history = (bar(0), bar(1), bar(2))
    assert repair.record_without_decision(history[0], recorded_at=NOW)
    assert repair.record_without_decision(history[1], recorded_at=NOW)
    ticket = marketdata.record_finalized_for_strategy(
        history[2],
        strategy_id=STRATEGY,
        recorded_at=NOW + timedelta(seconds=1),
    )
    if with_checkpoint:
        checkpoint_id = continuity_checkpoint_id(
            previous_checkpoint_id=None,
            provider="BYBIT",
            venue="BYBIT_LINEAR",
            symbol="BTCUSDT",
            interval_seconds=300,
            through_bar_id=history[2].bar_id,
            through_close_time=history[2].close_time,
            evidence_source="TEST",
        )
        checkpoint = OperationalContinuityCheckpoint(
            checkpoint_id=checkpoint_id,
            previous_checkpoint_id=None,
            provider="BYBIT",
            venue="BYBIT_LINEAR",
            symbol="BTCUSDT",
            interval_seconds=300,
            through_bar_id=history[2].bar_id,
            through_close_time=history[2].close_time,
            established_at=NOW,
            evidence_source="TEST",
        )
        assert continuity.append(checkpoint)
    leases = SQLiteDecisionLeaseStore(marketdata_path)
    control = SQLitePaperDispatchControlStore(tmp_path / "oms.sqlite")
    return history, ticket, marketdata, continuity, leases, control


def worker(
    *,
    marketdata,
    continuity,
    leases,
    control,
    planner,
    snapshot_provider,
    clock: MutableClock | None = None,
    policy: OperationalDecisionWorkerPolicy | None = None,
):
    resolved_clock = MutableClock(NOW + timedelta(seconds=2)) if clock is None else clock
    return OperationalDecisionWorker(
        strategy_id=STRATEGY,
        owner_id="worker-a",
        release_identity="release-a",
        leases=leases,
        marketdata=marketdata,
        continuity=continuity,
        readiness=OperationalReadinessEvaluator(),
        snapshot_provider=snapshot_provider,
        control=control,
        risk_context_provider=FakeRiskContextProvider(),
        planner=planner,
        clock=resolved_clock,
        policy=policy,
    )


def test_ready_worker_evaluates_one_durable_ticket_once(tmp_path) -> None:
    history, ticket, marketdata, continuity, leases, control = build_stack(tmp_path)
    planner = FakePlanner()
    service = worker(
        marketdata=marketdata,
        continuity=continuity,
        leases=leases,
        control=control,
        planner=planner,
        snapshot_provider=ready_snapshot,
    )

    result = service.run_next()

    assert result is not None and result.status == "COMPLETED"
    assert result.receipt.ticket.ticket_id == ticket.ticket_id
    assert result.safety.ready_for_evaluation
    assert result.safety.bar_ids == tuple(value.bar_id for value in history)
    assert result.safety.control_mode == "HALTED"
    assert planner.calls == 1
    assert marketdata.pending_decisions(strategy_id=STRATEGY) == ()
    assert service.run_next() is None
    assert planner.calls == 1


def test_degraded_readiness_blocks_planner_releases_ticket_and_halts_armed_control(tmp_path) -> None:
    _, ticket, marketdata, continuity, leases, control = build_stack(tmp_path)
    control.arm(
        operator_id="test-operator",
        reason="qualification",
        occurred_at=NOW,
        ttl=timedelta(minutes=1),
    )
    planner = FakePlanner()
    service = worker(
        marketdata=marketdata,
        continuity=continuity,
        leases=leases,
        control=control,
        planner=planner,
        snapshot_provider=lambda: ready_snapshot(market_data_ready=False),
    )

    result = service.run_next()

    assert result is not None and result.status == "BLOCKED"
    assert "MARKET_DATA_NOT_READY" in result.safety.readiness_reasons
    assert planner.calls == 0
    assert control.current().mode is DispatchControlMode.HALTED
    assert marketdata.pending_decisions(strategy_id=STRATEGY) == (ticket,)
    reclaimed = leases.claim_next(
        strategy_id=STRATEGY,
        owner_id="worker-b",
        release_identity="release-b",
        occurred_at=NOW + timedelta(seconds=2),
    )
    assert reclaimed is not None and reclaimed.fencing_token == 2


def test_missing_continuity_blocks_before_planner(tmp_path) -> None:
    _, _, marketdata, continuity, leases, control = build_stack(
        tmp_path,
        with_checkpoint=False,
    )
    planner = FakePlanner()
    service = worker(
        marketdata=marketdata,
        continuity=continuity,
        leases=leases,
        control=control,
        planner=planner,
        snapshot_provider=ready_snapshot,
    )

    result = service.run_next()

    assert result is not None and result.status == "BLOCKED"
    assert result.safety.continuity_reasons == ("CONTINUITY_CHECKPOINT_REQUIRED",)
    assert planner.calls == 0


class ConflictAfterClaimLeases:
    def __init__(self, delegate, marketdata) -> None:
        self.delegate = delegate
        self.marketdata = marketdata
        self.injected = False

    def claim_next(self, **kwargs):
        receipt = self.delegate.claim_next(**kwargs)
        if receipt is not None and not self.injected:
            self.injected = True
            conflicting = bar(2, close="999")
            try:
                self.marketdata.record_finalized_for_strategy(
                    conflicting,
                    strategy_id=STRATEGY,
                    recorded_at=kwargs["occurred_at"],
                )
            except OperationalBarConflict:
                pass
        return receipt

    def __getattr__(self, name):
        return getattr(self.delegate, name)


def test_late_conflict_after_claim_is_rechecked_and_blocks_planner(tmp_path) -> None:
    _, _, marketdata, continuity, leases, control = build_stack(tmp_path)
    planner = FakePlanner()
    race_leases = ConflictAfterClaimLeases(leases, marketdata)
    service = worker(
        marketdata=marketdata,
        continuity=continuity,
        leases=race_leases,
        control=control,
        planner=planner,
        snapshot_provider=ready_snapshot,
    )

    result = service.run_next()

    assert result is not None and result.status == "BLOCKED"
    assert "CONTINUITY_HIGH_WATER_CONFLICTED_OR_MISSING" in result.safety.continuity_reasons
    assert "DECISION_BAR_NOT_WINDOW_TAIL" in result.safety.continuity_reasons
    assert planner.calls == 0


def test_middle_bar_conflict_during_planning_invalidates_completion(tmp_path) -> None:
    _, ticket, marketdata, continuity, leases, control = build_stack(tmp_path)
    planner = ConflictDuringPlanner(marketdata)
    service = worker(
        marketdata=marketdata,
        continuity=continuity,
        leases=leases,
        control=control,
        planner=planner,
        snapshot_provider=ready_snapshot,
    )

    with pytest.raises(ValueError, match="DECISION_SAFETY_EVIDENCE_INVALIDATED"):
        service.run_next()

    assert planner.calls == 1
    assert marketdata.pending_decisions(strategy_id=STRATEGY) == (ticket,)


def test_slow_planner_cannot_complete_after_renewed_lease_expires(tmp_path) -> None:
    _, ticket, marketdata, continuity, leases, control = build_stack(tmp_path)
    clock = MutableClock(NOW + timedelta(seconds=2))
    planner = SlowPlanner(clock, timedelta(seconds=2))
    policy = OperationalDecisionWorkerPolicy(
        lease=DecisionLeasePolicy(lease_ttl=timedelta(seconds=1))
    )
    service = worker(
        marketdata=marketdata,
        continuity=continuity,
        leases=leases,
        control=control,
        planner=planner,
        snapshot_provider=ready_snapshot,
        clock=clock,
        policy=policy,
    )

    with pytest.raises(StaleDecisionLease, match="expired"):
        service.run_next()

    assert planner.calls == 1
    assert marketdata.pending_decisions(strategy_id=STRATEGY) == (ticket,)
    reclaimed = leases.claim_next(
        strategy_id=STRATEGY,
        owner_id="worker-b",
        release_identity="release-b",
        occurred_at=clock.value,
        policy=policy.lease,
    )
    assert reclaimed is not None
    assert reclaimed.ticket.ticket_id == ticket.ticket_id
    assert reclaimed.fencing_token == 2
