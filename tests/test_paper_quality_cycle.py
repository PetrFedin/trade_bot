from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.application.paper_execution_quality import (
    PaperExecutionQualityFill,
    SQLitePaperExecutionQualityStore,
)
from app.application.paper_quality_cycle import (
    QualityManagedCrossSectionalPaperCycleService,
)
from app.application.paper_quality_gate import ExecutionQualityGatePolicy
from app.domain.trading import Side
from app.strategy.quality_monitor import (
    StrategyQualityGateDecision,
    StrategyQualityStatus,
    TradeQualityMonitorPolicy,
    TradeQualityWindow,
)

NOW = datetime(2026, 8, 12, 18, 0, tzinfo=UTC)
STRATEGY = "cross-sectional-quality-v2-paper-shadow"


def trade_policy() -> TradeQualityMonitorPolicy:
    return TradeQualityMonitorPolicy(
        window_trades=20,
        minimum_observations=10,
        minimum_profit_factor=Decimal("1"),
        minimum_profit_preservation_rate=Decimal("0.50"),
        minimum_average_mfe_capture_ratio=Decimal("0.10"),
        maximum_hard_stop_fraction=Decimal("0.50"),
        maximum_consecutive_losses=4,
        allow_entries_when_insufficient_data=False,
    )


def healthy_trade_gate() -> StrategyQualityGateDecision:
    return StrategyQualityGateDecision(
        status=StrategyQualityStatus.HEALTHY,
        allow_new_entries=True,
        allow_exits=True,
        reasons=(),
        metrics=TradeQualityWindow(
            observation_count=20,
            winning_trades=12,
            losing_trades=8,
            breakeven_trades=0,
            gross_profit=Decimal("120"),
            gross_loss=Decimal("-60"),
            total_pnl=Decimal("60"),
            win_rate=Decimal("0.60"),
            profit_factor=Decimal("2"),
            positive_mfe_trades=18,
            positive_mfe_closed_profitable=12,
            profit_preservation_rate=Decimal("0.6666666667"),
            average_mfe_capture_ratio=Decimal("0.45"),
            hard_stop_fraction=Decimal("0.20"),
            current_consecutive_losses=0,
        ),
    )


class FakeTradeQuality:
    def __init__(self, *, strategy_id: str = STRATEGY) -> None:
        self.strategy_id = strategy_id
        self.policies = []

    def quality_gate(self, *, policy: TradeQualityMonitorPolicy):
        self.policies.append(policy)
        return healthy_trade_gate()


class FakeAuditedCycle:
    def __init__(self) -> None:
        self.cycle = SimpleNamespace(
            target_planner=SimpleNamespace(strategy_id=STRATEGY)
        )
        self.calls = []

    def plan_and_prepare(self, bars, **kwargs):
        self.calls.append((tuple(bars), kwargs))
        return SimpleNamespace(prepared_orders=())


def append_bad_execution(
    store: SQLitePaperExecutionQualityStore,
    *,
    index: int,
) -> None:
    expected = Decimal("100")
    fraction = Decimal("0.002")
    store.append(
        PaperExecutionQualityFill(
            fill_id=f"fill-{index}",
            intent_id=f"intent-{index}",
            symbol="AAPL",
            side=Side.BUY,
            quantity=Decimal("1"),
            expected_limit_price=expected,
            fill_price=Decimal("100.20"),
            signed_slippage_fraction=fraction,
            signed_slippage_notional=Decimal("0.20"),
            occurred_at=NOW + timedelta(seconds=index),
        )
    )


def test_cycle_derives_degraded_execution_gate_before_strategy_plan(
    tmp_path: Path,
) -> None:
    execution = SQLitePaperExecutionQualityStore(tmp_path / "execution.sqlite")
    for index in range(1, 4):
        append_bad_execution(execution, index=index)
    audited = FakeAuditedCycle()
    trade_quality = FakeTradeQuality()
    service = QualityManagedCrossSectionalPaperCycleService(
        cycle=audited,
        trade_quality=trade_quality,
        trade_policy=trade_policy(),
        execution_store=execution,
        execution_policy=ExecutionQualityGatePolicy(
            window_fills=5,
            minimum_observations=3,
            maximum_weighted_signed_slippage_bps=Decimal("5"),
            maximum_worst_signed_slippage_bps=Decimal("15"),
        ),
    )

    result = service.plan_and_prepare(
        (),
        reference_prices={"AAPL": Decimal("100")},
        generated_at=NOW,
    )

    assert result.quality_gate.allow_new_entries is False
    assert result.quality_gate.allow_exits is True
    assert "EXECUTION:WEIGHTED_SLIPPAGE_ABOVE_MAXIMUM" in (
        result.quality_gate.reasons
    )
    assert len(trade_quality.policies) == 1
    assert len(audited.calls) == 1
    applied_gate = audited.calls[0][1]["quality_gate"]
    assert applied_gate is result.quality_gate


def test_cycle_rejects_trade_quality_from_another_strategy() -> None:
    with pytest.raises(ValueError, match="must share strategy_id"):
        QualityManagedCrossSectionalPaperCycleService(
            cycle=FakeAuditedCycle(),
            trade_quality=FakeTradeQuality(strategy_id="other-strategy"),
            trade_policy=trade_policy(),
        )
