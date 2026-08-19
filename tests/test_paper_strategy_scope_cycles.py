from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.application.cross_sectional_paper_cycle import CrossSectionalPaperCycleService
from app.application.cross_sectional_target_planner import CrossSectionalTargetPlanner
from app.application.order_lifecycle import PaperOrderLifecycle
from app.application.paper_protection import (
    PaperProtectionService,
    SQLitePaperProtectionStore,
)
from app.application.paper_protection_cycle import PaperProtectionOrderService
from app.application.paper_strategy_scope import SQLitePaperStrategyIntentRegistry
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

NOW = datetime(2026, 8, 12, 2, 0, tzinfo=UTC)
STRATEGY = "cross-sectional-quality-v2-paper-shadow"


class FakeSelector:
    top_k = 2

    def select(self, bars) -> CrossSectionalSelection:
        del bars
        candidates = tuple(
            SelectionCandidate(
                rank=index,
                symbol=symbol,
                eligible=True,
                rejection_reasons=(),
                momentum_return=Decimal("0.03"),
                trend_strength=Decimal("0.02"),
                realized_volatility=Decimal("0.01"),
                quality_score=Decimal("0.04"),
                reference_price=Decimal("100"),
            )
            for index, symbol in enumerate(("AAPL", "MSFT"), start=1)
        )
        return CrossSectionalSelection(
            decision_time=NOW,
            selected_symbols=("AAPL", "MSFT"),
            candidates=candidates,
        )


def risk_engine() -> PreTradeRiskEngine:
    return PreTradeRiskEngine(
        RiskLimits(
            maximum_order_notional=Decimal("4000"),
            maximum_symbol_notional=Decimal("4000"),
            maximum_gross_notional=Decimal("6000"),
        )
    )


def portfolio_policy() -> CrossSectionalPortfolioPolicy:
    return CrossSectionalPortfolioPolicy(
        opening_cash=Decimal("10000"),
        maximum_gross_exposure_fraction=Decimal("0.60"),
        new_position_target_equity_fraction=Decimal("0.29"),
    )


def protection_policy() -> PositionManagementPolicy:
    return PositionManagementPolicy(
        stop_loss_fraction=Decimal("0.02"),
        take_profit_fraction=Decimal("0.10"),
        trailing_activation_fraction=Decimal("0.08"),
        trailing_stop_fraction=Decimal("0.015"),
        maximum_holding_bars=10,
        break_even_activation_fraction=Decimal("0.01"),
        break_even_buffer_fraction=Decimal("0.001"),
        profit_protection_activation_fraction=Decimal("0.015"),
        maximum_profit_giveback_fraction=Decimal("0.50"),
    )


def test_cross_sectional_cycle_registers_all_approved_intents_before_outbox(
    tmp_path: Path,
) -> None:
    ledger = PortfolioLedger(opening_cash=Decimal("10000"))
    oms = DurableOmsStore(tmp_path / "oms.sqlite")
    registry = SQLitePaperStrategyIntentRegistry(tmp_path / "scope.sqlite")
    cycle = CrossSectionalPaperCycleService(
        target_planner=CrossSectionalTargetPlanner(
            selector=FakeSelector(),
            portfolio_policy=portfolio_policy(),
            position_policy=PositionManagementPolicy(),
        ),
        order_planner=PortfolioPaperPlanner(ledger=ledger, risk=risk_engine()),
        lifecycle=PaperOrderLifecycle(oms),
        intent_registry=registry,
    )

    result = cycle.plan_and_prepare(
        (),
        reference_prices={"AAPL": Decimal("100"), "MSFT": Decimal("100")},
        generated_at=NOW + timedelta(seconds=1),
    )

    assert len(result.prepared_orders) == 2
    for prepared in result.prepared_orders:
        ownership = registry.get(prepared.record.intent_id)
        assert ownership is not None
        assert ownership.strategy_id == STRATEGY
        assert ownership.symbol == prepared.record.symbol
        assert ownership.side is prepared.record.side
    assert len(oms.pending_outbox()) == 2

    replay = cycle.plan_and_prepare(
        (),
        reference_prices={"AAPL": Decimal("100"), "MSFT": Decimal("100")},
        generated_at=NOW + timedelta(seconds=1),
    )
    assert [order.record.intent_id for order in replay.prepared_orders] == [
        order.record.intent_id for order in result.prepared_orders
    ]
    assert len(oms.pending_outbox()) == 2


def test_protection_retry_repairs_missing_strategy_ownership(tmp_path: Path) -> None:
    ledger = PortfolioLedger(opening_cash=Decimal("1000"))
    ledger.apply_fill(
        Fill(
            fill_id="entry-fill",
            order_intent_id="entry-intent",
            symbol="AAPL",
            side=Side.BUY,
            quantity=Decimal("1"),
            price=Decimal("100"),
            occurred_at=NOW - timedelta(minutes=1),
        )
    )
    oms = DurableOmsStore(tmp_path / "protection-oms.sqlite")
    protection = PaperProtectionService(
        ledger=ledger,
        store=SQLitePaperProtectionStore(tmp_path / "protection-state.sqlite"),
        policy=protection_policy(),
    )
    planner = PortfolioPaperPlanner(ledger=ledger, risk=risk_engine())
    lifecycle = PaperOrderLifecycle(oms)
    unscoped = PaperProtectionOrderService(
        protection=protection,
        planner=planner,
        lifecycle=lifecycle,
    )
    unscoped.observe_and_prepare(
        symbol="AAPL",
        reference_price=Decimal("102"),
        observed_at=NOW,
        mark_prices={"AAPL": Decimal("102")},
    )
    first = unscoped.observe_and_prepare(
        symbol="AAPL",
        reference_price=Decimal("101"),
        observed_at=NOW + timedelta(seconds=1),
        mark_prices={"AAPL": Decimal("101")},
    )
    assert first.prepared is not None

    registry = SQLitePaperStrategyIntentRegistry(tmp_path / "scope.sqlite")
    assert registry.get(first.prepared.record.intent_id) is None
    scoped = PaperProtectionOrderService(
        protection=protection,
        planner=planner,
        lifecycle=lifecycle,
        intent_registry=registry,
    )
    replay = scoped.observe_and_prepare(
        symbol="AAPL",
        reference_price=Decimal("100.5"),
        observed_at=NOW + timedelta(seconds=2),
        mark_prices={"AAPL": Decimal("100.5")},
    )

    assert replay.existing_order_reused is True
    assert replay.prepared is not None
    assert replay.prepared.record.intent_id == first.prepared.record.intent_id
    ownership = registry.get(first.prepared.record.intent_id)
    assert ownership is not None
    assert ownership.strategy_id == STRATEGY
    assert ownership.symbol == "AAPL"
    assert ownership.side is Side.SELL
    assert len(oms.pending_outbox()) == 1
