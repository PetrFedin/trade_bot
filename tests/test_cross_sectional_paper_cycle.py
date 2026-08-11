from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.application.cross_sectional_paper_cycle import CrossSectionalPaperCycleService
from app.application.cross_sectional_target_planner import CrossSectionalTargetPlanner
from app.application.order_lifecycle import PaperOrderLifecycle
from app.application.paper_trade_quality import (
    PaperTradeQualityTracker,
    SQLitePaperTradeQualityStore,
)
from app.application.portfolio_paper_planner import PortfolioPaperPlanner
from app.domain.trading import Fill, Side
from app.oms.store import DurableOmsStore
from app.portfolio.ledger import PortfolioLedger
from app.risk.pretrade import PreTradeRiskEngine, RiskLimits
from app.strategy.cross_sectional_portfolio import CrossSectionalPortfolioPolicy
from app.strategy.cross_sectional_selection import (
    CrossSectionalSelection,
    SelectionCandidate,
)
from app.strategy.position_management import PositionManagementPolicy

NOW = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Gate:
    allow_new_entries: bool
    allow_exits: bool = True
    reasons: tuple[str, ...] = ()


def candidate(symbol: str, rank: int | None) -> SelectionCandidate:
    return SelectionCandidate(
        rank=rank,
        symbol=symbol,
        eligible=True,
        rejection_reasons=(),
        momentum_return=Decimal("0.03"),
        trend_strength=Decimal("0.02"),
        realized_volatility=Decimal("0.01"),
        quality_score=Decimal("0.04"),
        reference_price=Decimal("100"),
    )


class FakeSelector:
    top_k = 2

    def select(self, bars) -> CrossSectionalSelection:
        del bars
        return CrossSectionalSelection(
            decision_time=NOW,
            selected_symbols=("AAPL", "MSFT"),
            candidates=(
                candidate("AAPL", 1),
                candidate("MSFT", 2),
                candidate("NVDA", 3),
            ),
        )


def policy() -> CrossSectionalPortfolioPolicy:
    return CrossSectionalPortfolioPolicy(
        opening_cash=Decimal("10000"),
        fee_per_fill=Decimal("0"),
        slippage_bps=Decimal("0"),
        maximum_gross_exposure_fraction=Decimal("0.60"),
        new_position_target_equity_fraction=Decimal("0.29"),
    )


def risk() -> PreTradeRiskEngine:
    return PreTradeRiskEngine(
        RiskLimits(
            maximum_order_notional=Decimal("4000"),
            maximum_symbol_notional=Decimal("4000"),
            maximum_gross_notional=Decimal("6000"),
        )
    )


def seed_nvda(ledger: PortfolioLedger) -> Fill:
    fill = Fill(
        fill_id="seed-nvda",
        order_intent_id="seed-nvda-intent",
        symbol="NVDA",
        side=Side.BUY,
        quantity=Decimal("10"),
        price=Decimal("100"),
        occurred_at=NOW - timedelta(minutes=1),
    )
    ledger.apply_fill(fill)
    return fill


def build_cycle(tmp_path: Path):
    ledger = PortfolioLedger(opening_cash=Decimal("10000"))
    quality = PaperTradeQualityTracker(
        store=SQLitePaperTradeQualityStore(tmp_path / "quality.sqlite")
    )
    target_planner = CrossSectionalTargetPlanner(
        selector=FakeSelector(),
        portfolio_policy=policy(),
        position_policy=PositionManagementPolicy(),
    )
    lifecycle = PaperOrderLifecycle(DurableOmsStore(tmp_path / "oms.sqlite"))
    cycle = CrossSectionalPaperCycleService(
        target_planner=target_planner,
        order_planner=PortfolioPaperPlanner(ledger=ledger, risk=risk()),
        lifecycle=lifecycle,
        quality_recorder=quality,
    )
    return ledger, quality, lifecycle, cycle


def prices() -> dict[str, Decimal]:
    return {
        "AAPL": Decimal("100"),
        "MSFT": Decimal("100"),
        "NVDA": Decimal("100"),
    }


def test_cycle_prepares_exit_before_entry_without_spending_unfilled_exit(
    tmp_path: Path,
) -> None:
    ledger, quality, lifecycle, cycle = build_cycle(tmp_path)
    seed = seed_nvda(ledger)
    quality.observe_fill(seed)

    result = cycle.plan_and_prepare(
        (),
        reference_prices=prices(),
        generated_at=NOW + timedelta(seconds=1),
    )

    assert result.target_plan.selected_symbols == ("AAPL", "MSFT")
    assert result.target_plan.entry_blocks == (
        ("MSFT", result.target_plan.entry_blocks[0][1]),
    )
    assert result.target_plan.entry_blocks[0][1].value == "GROSS_EXPOSURE_CAP"
    assert result.prepared_exit_count == 1
    assert result.prepared_entry_count == 1
    assert [order.record.side for order in result.prepared_orders] == [
        Side.SELL,
        Side.BUY,
    ]
    assert [order.record.symbol for order in result.prepared_orders] == [
        "NVDA",
        "AAPL",
    ]
    assert len(lifecycle.store.pending_outbox()) == 2

    exit_order = result.prepared_orders[0]
    quality.observe_fill(
        Fill(
            fill_id="nvda-exit-fill",
            order_intent_id=exit_order.record.intent_id,
            symbol="NVDA",
            side=Side.SELL,
            quantity=Decimal("10"),
            price=Decimal("101"),
            occurred_at=NOW + timedelta(seconds=2),
        )
    )
    closed = quality.store.closed_trades(strategy_id=quality.strategy_id)
    assert len(closed) == 1
    assert closed[0].exit_reason == "SELECTION_EXIT"
    assert closed[0].net_pnl == Decimal("10")


def test_quality_pause_blocks_new_buy_but_preserves_selection_exit(tmp_path: Path) -> None:
    ledger, quality, lifecycle, cycle = build_cycle(tmp_path)
    seed = seed_nvda(ledger)
    quality.observe_fill(seed)

    result = cycle.plan_and_prepare(
        (),
        reference_prices=prices(),
        generated_at=NOW + timedelta(seconds=1),
        quality_gate=Gate(
            allow_new_entries=False,
            reasons=("PROFIT_FACTOR_BELOW_MINIMUM",),
        ),
    )

    assert result.prepared_exit_count == 1
    assert result.prepared_entry_count == 0
    assert len(result.prepared_orders) == 1
    assert result.prepared_orders[0].record.side is Side.SELL
    assert result.prepared_orders[0].record.symbol == "NVDA"
    assert len(lifecycle.store.pending_outbox()) == 1
    paused = [
        item
        for item in result.order_plan.items
        if item.intent is not None and item.intent.side is Side.BUY
    ]
    assert len(paused) == 1
    assert paused[0].reasons == (
        "QUALITY_GATE_PAUSE_ENTRIES",
        "PROFIT_FACTOR_BELOW_MINIMUM",
    )


def test_same_cycle_replay_is_idempotent_at_durable_outbox(tmp_path: Path) -> None:
    ledger, quality, lifecycle, cycle = build_cycle(tmp_path)
    seed = seed_nvda(ledger)
    quality.observe_fill(seed)
    generated_at = NOW + timedelta(seconds=1)

    first = cycle.plan_and_prepare(
        (),
        reference_prices=prices(),
        generated_at=generated_at,
    )
    second = cycle.plan_and_prepare(
        (),
        reference_prices=prices(),
        generated_at=generated_at,
    )

    assert [item.record.intent_id for item in first.prepared_orders] == [
        item.record.intent_id for item in second.prepared_orders
    ]
    assert [item.client_order_id for item in first.prepared_orders] == [
        item.client_order_id for item in second.prepared_orders
    ]
    assert len(lifecycle.store.pending_outbox()) == 2
