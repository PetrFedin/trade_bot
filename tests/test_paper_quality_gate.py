from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.application.paper_execution_quality import (
    PaperExecutionQualityFill,
    SQLitePaperExecutionQualityStore,
)
from app.application.paper_quality_gate import (
    ExecutionQualityGatePolicy,
    PaperQualityGateStatus,
    ReactionQualityGatePolicy,
    evaluate_paper_quality_gate,
)
from app.application.paper_reaction_quality import (
    PaperReactionFill,
    SQLitePaperReactionQualityStore,
)
from app.application.portfolio_paper_planner import (
    PortfolioPaperDisposition,
    PortfolioPaperPlanner,
)
from app.domain.trading import Fill, Side, TargetPosition
from app.portfolio.ledger import PortfolioLedger
from app.risk.pretrade import PreTradeRiskEngine, RiskLimits
from app.strategy.quality_monitor import (
    StrategyQualityGateDecision,
    StrategyQualityStatus,
    TradeQualityWindow,
)

NOW = datetime(2026, 8, 12, 17, 0, tzinfo=UTC)
STRATEGY = "cross-sectional-quality-v2-paper-shadow"


def trade_gate(*, allow_entries: bool = True) -> StrategyQualityGateDecision:
    return StrategyQualityGateDecision(
        status=(
            StrategyQualityStatus.HEALTHY
            if allow_entries
            else StrategyQualityStatus.PAUSE_ENTRIES
        ),
        allow_new_entries=allow_entries,
        allow_exits=True,
        reasons=() if allow_entries else ("PROFIT_FACTOR_BELOW_MINIMUM",),
        metrics=TradeQualityWindow(
            observation_count=20,
            winning_trades=12,
            losing_trades=8,
            breakeven_trades=0,
            gross_profit=Decimal("120"),
            gross_loss=Decimal("-60"),
            total_pnl=Decimal("60"),
            win_rate=Decimal("0.6"),
            profit_factor=Decimal("2"),
            positive_mfe_trades=18,
            positive_mfe_closed_profitable=12,
            profit_preservation_rate=Decimal("0.6666666667"),
            average_mfe_capture_ratio=Decimal("0.45"),
            hard_stop_fraction=Decimal("0.20"),
            current_consecutive_losses=0,
        ),
    )


def execution_policy() -> ExecutionQualityGatePolicy:
    return ExecutionQualityGatePolicy(
        window_fills=5,
        minimum_observations=3,
        maximum_weighted_signed_slippage_bps=Decimal("5"),
        maximum_worst_signed_slippage_bps=Decimal("15"),
    )


def reaction_policy() -> ReactionQualityGatePolicy:
    return ReactionQualityGatePolicy(
        window_fills=5,
        minimum_observations=3,
        maximum_average_latency_seconds=Decimal("5"),
        maximum_p95_latency_seconds=Decimal("8"),
    )


def append_execution(
    store: SQLitePaperExecutionQualityStore,
    *,
    index: int,
    signed_bps: str,
) -> None:
    expected = Decimal("100")
    fraction = Decimal(signed_bps) / Decimal("10000")
    store.append(
        PaperExecutionQualityFill(
            fill_id=f"execution-{index}",
            intent_id=f"intent-{index}",
            symbol="AAPL",
            side=Side.BUY,
            quantity=Decimal("1"),
            expected_limit_price=expected,
            fill_price=expected * (Decimal("1") + fraction),
            signed_slippage_fraction=fraction,
            signed_slippage_notional=fraction * expected,
            occurred_at=NOW + timedelta(seconds=index),
        )
    )


def append_reaction(
    store: SQLitePaperReactionQualityStore,
    *,
    index: int,
    latency: str,
) -> None:
    latency_decimal = Decimal(latency)
    store.append(
        PaperReactionFill(
            fill_id=f"reaction-{index}",
            intent_id=f"intent-{index}",
            strategy_id=STRATEGY,
            symbol="AAPL",
            side=Side.BUY,
            decision_at=NOW,
            fill_at=NOW + timedelta(seconds=int(latency_decimal)),
            latency_seconds=latency_decimal,
        )
    )


def test_healthy_trade_execution_and_reaction_allow_new_entries(tmp_path: Path) -> None:
    execution = SQLitePaperExecutionQualityStore(tmp_path / "execution.sqlite")
    reaction = SQLitePaperReactionQualityStore(tmp_path / "reaction.sqlite")
    for index, bps in enumerate(("1", "0", "-1"), start=1):
        append_execution(execution, index=index, signed_bps=bps)
    for index, latency in enumerate(("1", "2", "3"), start=1):
        append_reaction(reaction, index=index, latency=latency)

    decision = evaluate_paper_quality_gate(
        trade_gate=trade_gate(),
        execution_store=execution,
        execution_policy=execution_policy(),
        reaction_store=reaction,
        reaction_policy=reaction_policy(),
        strategy_id=STRATEGY,
    )

    assert decision.status is PaperQualityGateStatus.HEALTHY
    assert decision.allow_new_entries is True
    assert decision.allow_exits is True
    assert decision.reasons == ()
    assert decision.execution is not None
    assert decision.execution.observation_count == 3
    assert decision.reaction is not None
    assert decision.reaction.p95_latency_seconds == Decimal("3")


def test_bad_execution_pauses_entries_but_never_exits(tmp_path: Path) -> None:
    execution = SQLitePaperExecutionQualityStore(tmp_path / "execution.sqlite")
    for index, bps in enumerate(("20", "18", "22"), start=1):
        append_execution(execution, index=index, signed_bps=bps)

    decision = evaluate_paper_quality_gate(
        trade_gate=trade_gate(),
        execution_store=execution,
        execution_policy=execution_policy(),
    )

    assert decision.status is PaperQualityGateStatus.PAUSE_ENTRIES
    assert decision.allow_new_entries is False
    assert decision.allow_exits is True
    assert decision.reasons == (
        "EXECUTION:WEIGHTED_SLIPPAGE_ABOVE_MAXIMUM",
        "EXECUTION:WORST_SLIPPAGE_ABOVE_MAXIMUM",
    )


def test_slow_reaction_pauses_entries(tmp_path: Path) -> None:
    reaction = SQLitePaperReactionQualityStore(tmp_path / "reaction.sqlite")
    for index, latency in enumerate(("2", "3", "12"), start=1):
        append_reaction(reaction, index=index, latency=latency)

    decision = evaluate_paper_quality_gate(
        trade_gate=trade_gate(),
        reaction_store=reaction,
        reaction_policy=reaction_policy(),
        strategy_id=STRATEGY,
    )

    assert decision.status is PaperQualityGateStatus.PAUSE_ENTRIES
    assert decision.allow_new_entries is False
    assert decision.allow_exits is True
    assert "REACTION:P95_LATENCY_ABOVE_MAXIMUM" in decision.reasons


def test_insufficient_execution_evidence_fails_closed_by_default(tmp_path: Path) -> None:
    execution = SQLitePaperExecutionQualityStore(tmp_path / "execution.sqlite")
    append_execution(execution, index=1, signed_bps="0")

    decision = evaluate_paper_quality_gate(
        trade_gate=trade_gate(),
        execution_store=execution,
        execution_policy=execution_policy(),
    )

    assert decision.status is PaperQualityGateStatus.INSUFFICIENT_DATA
    assert decision.allow_new_entries is False
    assert decision.allow_exits is True
    assert decision.reasons == ("EXECUTION:INSUFFICIENT_OBSERVATIONS",)


def test_composite_gate_pauses_buy_while_portfolio_exit_stays_approved(
    tmp_path: Path,
) -> None:
    execution = SQLitePaperExecutionQualityStore(tmp_path / "execution.sqlite")
    for index, bps in enumerate(("20", "20", "20"), start=1):
        append_execution(execution, index=index, signed_bps=bps)
    gate = evaluate_paper_quality_gate(
        trade_gate=trade_gate(),
        execution_store=execution,
        execution_policy=execution_policy(),
    )

    ledger = PortfolioLedger(opening_cash=Decimal("1000"))
    ledger.apply_fill(
        Fill(
            fill_id="seed-aapl",
            order_intent_id="seed-aapl-intent",
            symbol="AAPL",
            side=Side.BUY,
            quantity=Decimal("1"),
            price=Decimal("100"),
            occurred_at=NOW - timedelta(minutes=1),
        )
    )
    planner = PortfolioPaperPlanner(
        ledger=ledger,
        risk=PreTradeRiskEngine(
            RiskLimits(
                maximum_order_notional=Decimal("1000"),
                maximum_symbol_notional=Decimal("1000"),
                maximum_gross_notional=Decimal("2000"),
            )
        ),
    )
    plan = planner.plan(
        (
            TargetPosition(
                symbol="AAPL",
                quantity=Decimal("0"),
                reference_price=Decimal("100"),
                generated_at=NOW,
                strategy_id=STRATEGY,
            ),
            TargetPosition(
                symbol="MSFT",
                quantity=Decimal("1"),
                reference_price=Decimal("100"),
                generated_at=NOW,
                strategy_id=STRATEGY,
            ),
        ),
        mark_prices={"AAPL": Decimal("100"), "MSFT": Decimal("100")},
        quality_gate=gate,
    )

    assert plan.approved_exit_count == 1
    assert plan.approved_entry_count == 0
    paused = next(item for item in plan.items if item.target.symbol == "MSFT")
    assert paused.disposition is PortfolioPaperDisposition.ENTRY_PAUSED
    assert "QUALITY_GATE_PAUSE_ENTRIES" in paused.reasons
