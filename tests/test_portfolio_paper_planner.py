from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.application.order_intents import order_intent_for_target
from app.application.order_lifecycle import PaperOrderLifecycle
from app.application.portfolio_paper_planner import (
    PortfolioPaperDisposition,
    PortfolioPaperPlanner,
    prepare_approved_paper_orders,
)
from app.domain.trading import Fill, Side, TargetPosition
from app.oms.store import DurableOmsStore, OrderState
from app.portfolio.ledger import PortfolioLedger
from app.risk.pretrade import PreTradeRiskEngine, RiskLimits

NOW = datetime(2026, 8, 11, 20, 30, tzinfo=UTC)


@dataclass(frozen=True)
class Gate:
    allow_new_entries: bool
    allow_exits: bool = True
    reasons: tuple[str, ...] = ()


def risk_engine(*, maximum_gross: str = "10000") -> PreTradeRiskEngine:
    return PreTradeRiskEngine(
        RiskLimits(
            maximum_order_notional=Decimal("5000"),
            maximum_symbol_notional=Decimal("5000"),
            maximum_gross_notional=Decimal(maximum_gross),
        )
    )


def target(
    symbol: str,
    quantity: str,
    price: str,
    *,
    generated_at: datetime = NOW,
) -> TargetPosition:
    return TargetPosition(
        symbol=symbol,
        quantity=Decimal(quantity),
        reference_price=Decimal(price),
        generated_at=generated_at,
        strategy_id="cross-sectional-quality-v2",
    )


def seed_long(
    ledger: PortfolioLedger,
    *,
    symbol: str,
    quantity: str,
    price: str,
) -> None:
    ledger.apply_fill(
        Fill(
            fill_id=f"seed-{symbol}",
            order_intent_id=f"seed-intent-{symbol}",
            symbol=symbol,
            side=Side.BUY,
            quantity=Decimal(quantity),
            price=Decimal(price),
            occurred_at=NOW - timedelta(minutes=1),
        )
    )


def test_shared_intent_factory_is_deterministic_and_skips_noop() -> None:
    planned = target("AAPL", "2", "101")
    first = order_intent_for_target(planned, current_quantity=Decimal("1"))
    second = order_intent_for_target(planned, current_quantity=Decimal("1"))

    assert first is not None
    assert first == second
    assert first.side is Side.BUY
    assert first.quantity == Decimal("1")
    assert order_intent_for_target(planned, current_quantity=Decimal("2")) is None


def test_quality_pause_blocks_entry_but_preserves_and_outboxes_exit(tmp_path) -> None:
    ledger = PortfolioLedger(opening_cash=Decimal("1000"))
    seed_long(ledger, symbol="AAPL", quantity="1", price="100")
    planner = PortfolioPaperPlanner(ledger=ledger, risk=risk_engine())

    plan = planner.plan(
        (
            target("MSFT", "2", "50"),
            target("AAPL", "0", "100"),
        ),
        mark_prices={"AAPL": Decimal("100"), "MSFT": Decimal("50")},
        quality_gate=Gate(
            allow_new_entries=False,
            reasons=("CONSECUTIVE_LOSS_LIMIT_REACHED",),
        ),
    )

    assert [item.target.symbol for item in plan.items] == ["AAPL", "MSFT"]
    exit_item, entry_item = plan.items
    assert exit_item.disposition is PortfolioPaperDisposition.RISK_APPROVED
    assert exit_item.intent is not None and exit_item.intent.side is Side.SELL
    assert entry_item.disposition is PortfolioPaperDisposition.ENTRY_PAUSED
    assert entry_item.risk is None
    assert entry_item.reasons == (
        "QUALITY_GATE_PAUSE_ENTRIES",
        "CONSECUTIVE_LOSS_LIMIT_REACHED",
    )
    assert plan.approved_exit_count == 1
    assert plan.approved_entry_count == 0

    store = DurableOmsStore(tmp_path / "portfolio-paper.sqlite")
    prepared = prepare_approved_paper_orders(
        plan,
        lifecycle=PaperOrderLifecycle(store),
    )
    assert len(prepared) == 1
    assert prepared[0].record.side is Side.SELL
    assert prepared[0].record.state is OrderState.OUTBOXED
    assert len(store.pending_outbox()) == 1


def test_approved_exit_does_not_fund_unfilled_new_entry() -> None:
    ledger = PortfolioLedger(opening_cash=Decimal("100"))
    seed_long(ledger, symbol="AAPL", quantity="1", price="100")
    assert ledger.cash == 0
    planner = PortfolioPaperPlanner(ledger=ledger, risk=risk_engine())

    plan = planner.plan(
        (
            target("MSFT", "2", "50"),
            target("AAPL", "0", "100"),
        ),
        mark_prices={"AAPL": Decimal("100"), "MSFT": Decimal("50")},
        quality_gate=Gate(allow_new_entries=True),
    )

    exit_item, entry_item = plan.items
    assert exit_item.risk is not None and exit_item.risk.approved
    assert entry_item.risk is not None and not entry_item.risk.approved
    assert entry_item.risk.reasons == ("INSUFFICIENT_AVAILABLE_CASH",)
    assert plan.reserved_buy_notional == 0
    assert plan.reserved_turnover_notional == Decimal("100")


def test_approved_buys_reserve_cash_and_gross_for_later_ranked_entries() -> None:
    ledger = PortfolioLedger(opening_cash=Decimal("1000"))
    planner = PortfolioPaperPlanner(ledger=ledger, risk=risk_engine())

    plan = planner.plan(
        (
            target("AAPL", "6", "100"),
            target("MSFT", "5", "100"),
        ),
        mark_prices={"AAPL": Decimal("100"), "MSFT": Decimal("100")},
        quality_gate=Gate(allow_new_entries=True),
    )

    first, second = plan.items
    assert first.target.symbol == "AAPL"
    assert first.risk is not None and first.risk.approved
    assert second.target.symbol == "MSFT"
    assert second.risk is not None and not second.risk.approved
    assert second.risk.reasons == ("INSUFFICIENT_AVAILABLE_CASH",)
    assert plan.reserved_buy_notional == Decimal("600")
    assert plan.reserved_turnover_notional == Decimal("600")


def test_portfolio_planner_fails_closed_on_ambiguous_target_batch() -> None:
    ledger = PortfolioLedger(opening_cash=Decimal("1000"))
    planner = PortfolioPaperPlanner(ledger=ledger, risk=risk_engine())

    with pytest.raises(ValueError, match="duplicate portfolio target"):
        planner.plan(
            (target("AAPL", "1", "100"), target("AAPL", "2", "100")),
            mark_prices={"AAPL": Decimal("100")},
        )

    with pytest.raises(ValueError, match="one decision timestamp"):
        planner.plan(
            (
                target("AAPL", "1", "100"),
                target(
                    "MSFT",
                    "1",
                    "100",
                    generated_at=NOW + timedelta(seconds=1),
                ),
            ),
            mark_prices={"AAPL": Decimal("100"), "MSFT": Decimal("100")},
        )


def test_quality_gate_is_not_permitted_to_disable_exits() -> None:
    ledger = PortfolioLedger(opening_cash=Decimal("1000"))
    seed_long(ledger, symbol="AAPL", quantity="1", price="100")
    planner = PortfolioPaperPlanner(ledger=ledger, risk=risk_engine())

    with pytest.raises(ValueError, match="QUALITY_GATE_MUST_NOT_BLOCK_EXITS"):
        planner.plan(
            (target("AAPL", "0", "100"),),
            mark_prices={"AAPL": Decimal("100")},
            quality_gate=Gate(allow_new_entries=False, allow_exits=False),
        )
