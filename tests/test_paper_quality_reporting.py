import json
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
)
from app.application.paper_quality_reporting import build_paper_trading_quality_report
from app.application.paper_reaction_quality import (
    PaperReactionFill,
    SQLitePaperReactionQualityStore,
)
from app.application.paper_trade_quality import (
    PaperTradeQualityTracker,
    SQLitePaperTradeQualityStore,
)
from app.domain.trading import Fill, Side
from app.strategy.quality_monitor import (
    StrategyQualityStatus,
    TradeQualityMonitorPolicy,
)

NOW = datetime(2026, 8, 12, 4, 30, tzinfo=UTC)
STRATEGY = "cross-sectional-quality-v2-paper-shadow"


def trade_fill(
    *,
    fill_id: str,
    intent_id: str,
    side: Side,
    price: str,
    occurred_at: datetime,
) -> Fill:
    return Fill(
        fill_id=fill_id,
        order_intent_id=intent_id,
        symbol="AAPL",
        side=side,
        quantity=Decimal("1"),
        price=Decimal(price),
        occurred_at=occurred_at,
    )


def policy() -> TradeQualityMonitorPolicy:
    return TradeQualityMonitorPolicy(
        window_trades=20,
        minimum_observations=2,
        minimum_profit_factor=Decimal("1"),
        minimum_profit_preservation_rate=Decimal("0.50"),
        minimum_average_mfe_capture_ratio=Decimal("0.10"),
        maximum_hard_stop_fraction=Decimal("0.50"),
        maximum_consecutive_losses=4,
        allow_entries_when_insufficient_data=False,
    )


def build_two_trades(tmp_path: Path) -> PaperTradeQualityTracker:
    tracker = PaperTradeQualityTracker(
        store=SQLitePaperTradeQualityStore(tmp_path / "trade-quality.sqlite"),
        strategy_id=STRATEGY,
    )
    tracker.observe_fill(
        trade_fill(
            fill_id="buy-1",
            intent_id="entry-1",
            side=Side.BUY,
            price="100",
            occurred_at=NOW,
        )
    )
    tracker.observe_price(
        symbol="AAPL",
        reference_price=Decimal("105"),
        observed_at=NOW + timedelta(seconds=1),
    )
    tracker.register_exit_intent(
        intent_id="exit-1",
        symbol="AAPL",
        exit_reason="PROFIT_PROTECTION",
        registered_at=NOW + timedelta(seconds=2),
    )
    tracker.observe_fill(
        trade_fill(
            fill_id="sell-1",
            intent_id="exit-1",
            side=Side.SELL,
            price="104",
            occurred_at=NOW + timedelta(seconds=3),
        )
    )

    tracker.observe_fill(
        trade_fill(
            fill_id="buy-2",
            intent_id="entry-2",
            side=Side.BUY,
            price="100",
            occurred_at=NOW + timedelta(seconds=4),
        )
    )
    tracker.register_exit_intent(
        intent_id="exit-2",
        symbol="AAPL",
        exit_reason="HARD_STOP",
        registered_at=NOW + timedelta(seconds=5),
    )
    tracker.observe_fill(
        trade_fill(
            fill_id="sell-2",
            intent_id="exit-2",
            side=Side.SELL,
            price="98",
            occurred_at=NOW + timedelta(seconds=6),
        )
    )
    return tracker


def execution_store(tmp_path: Path) -> SQLitePaperExecutionQualityStore:
    store = SQLitePaperExecutionQualityStore(tmp_path / "execution-quality.sqlite")
    store.append(
        PaperExecutionQualityFill(
            fill_id="execution-entry",
            intent_id="entry-1",
            symbol="AAPL",
            side=Side.BUY,
            quantity=Decimal("1"),
            expected_limit_price=Decimal("100"),
            fill_price=Decimal("100.10"),
            signed_slippage_fraction=Decimal("0.001"),
            signed_slippage_notional=Decimal("0.10"),
            occurred_at=NOW,
        )
    )
    store.append(
        PaperExecutionQualityFill(
            fill_id="execution-exit",
            intent_id="exit-1",
            symbol="AAPL",
            side=Side.SELL,
            quantity=Decimal("1"),
            expected_limit_price=Decimal("104"),
            fill_price=Decimal("103.80"),
            signed_slippage_fraction=Decimal("0.20") / Decimal("104"),
            signed_slippage_notional=Decimal("0.20"),
            occurred_at=NOW + timedelta(seconds=3),
        )
    )
    return store


def reaction_store(tmp_path: Path) -> SQLitePaperReactionQualityStore:
    store = SQLitePaperReactionQualityStore(tmp_path / "reaction-quality.sqlite")
    store.append(
        PaperReactionFill(
            fill_id="reaction-entry",
            intent_id="entry-1",
            strategy_id=STRATEGY,
            symbol="AAPL",
            side=Side.BUY,
            decision_at=NOW,
            fill_at=NOW + timedelta(seconds=1),
            latency_seconds=Decimal("1"),
        )
    )
    store.append(
        PaperReactionFill(
            fill_id="reaction-exit",
            intent_id="exit-1",
            strategy_id=STRATEGY,
            symbol="AAPL",
            side=Side.SELL,
            decision_at=NOW + timedelta(seconds=1),
            fill_at=NOW + timedelta(seconds=3),
            latency_seconds=Decimal("2"),
        )
    )
    return store


def test_report_separates_and_composes_trade_execution_reaction_quality(
    tmp_path: Path,
) -> None:
    tracker = build_two_trades(tmp_path)
    execution = execution_store(tmp_path)
    reaction = reaction_store(tmp_path)
    report = build_paper_trading_quality_report(
        tracker=tracker,
        policy=policy(),
        generated_at=NOW + timedelta(minutes=1),
        execution_store=execution,
        reaction_store=reaction,
        execution_gate_policy=ExecutionQualityGatePolicy(
            window_fills=5,
            minimum_observations=2,
            maximum_weighted_signed_slippage_bps=Decimal("10"),
            maximum_worst_signed_slippage_bps=Decimal("25"),
        ),
        reaction_gate_policy=ReactionQualityGatePolicy(
            window_fills=5,
            minimum_observations=2,
            maximum_average_latency_seconds=Decimal("5"),
            maximum_p95_latency_seconds=Decimal("5"),
        ),
    )

    assert report.trade_count == 2
    assert report.win_count == 1
    assert report.loss_count == 1
    assert report.win_rate == Decimal("0.5")
    assert report.total_net_pnl == Decimal("2")
    assert report.gross_profit == Decimal("4")
    assert report.gross_loss == Decimal("2")
    assert report.profit_factor == Decimal("2")
    assert report.positive_mfe_trade_count == 1
    assert report.profit_preserved_trade_count == 1
    assert report.profit_preservation_rate == Decimal("1")
    assert report.exit_reason_counts == (("HARD_STOP", 1), ("PROFIT_PROTECTION", 1))
    assert report.quality_gate.status is StrategyQualityStatus.HEALTHY
    assert report.execution_entries is not None
    assert report.execution_entries.signed_slippage_notional == Decimal("0.10")
    assert report.execution_exits is not None
    assert report.execution_exits.signed_slippage_notional == Decimal("0.20")
    assert report.reaction_all is not None
    assert report.reaction_all.average_latency_seconds == Decimal("1.5")
    assert report.reaction_exits is not None
    assert report.reaction_exits.p95_latency_seconds == Decimal("2")
    assert report.composite_quality_gate is not None
    assert report.composite_quality_gate.status is PaperQualityGateStatus.PAUSE_ENTRIES
    assert report.composite_quality_gate.allow_new_entries is False
    assert report.composite_quality_gate.allow_exits is True
    assert report.composite_quality_gate.reasons == (
        "EXECUTION:WEIGHTED_SLIPPAGE_ABOVE_MAXIMUM",
    )

    document = report.to_dict()
    assert document["total_net_pnl"] == "2"
    assert document["quality_gate"]["status"] == "HEALTHY"
    assert document["execution_exits"]["signed_slippage_notional"] == "0.20"
    assert document["reaction_all"]["average_latency_seconds"] == "1.5"
    assert document["composite_quality_gate"]["status"] == "PAUSE_ENTRIES"
    json.dumps(document)
