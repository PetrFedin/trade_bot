from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.application.cross_sectional_target_planner import CrossSectionalTargetPlanner
from app.application.portfolio_paper_planner import PortfolioPaperPlanner
from app.domain.trading import Fill, Side
from app.marketdata.ohlcv import OhlcvBar
from app.portfolio.ledger import PortfolioLedger
from app.risk.pretrade import PreTradeRiskEngine, RiskLimits
from app.strategy.cross_sectional_portfolio import (
    CrossSectionalPortfolioPolicy,
    PortfolioEntryBlockReason,
    PortfolioExitReason,
)
from app.strategy.cross_sectional_selection import CrossSectionalSelector
from app.strategy.position_management import PositionManagementPolicy

START = datetime(2026, 1, 2, tzinfo=UTC)


def series(symbol: str, closes: list[str]) -> list[OhlcvBar]:
    return [
        OhlcvBar(
            symbol=symbol,
            timestamp=START + timedelta(days=index),
            open=Decimal(close),
            high=Decimal(close) + Decimal("0.2"),
            low=Decimal(close) - Decimal("0.2"),
            close=Decimal(close),
            volume=1000 + index,
            trade_count=100 + index,
            vwap=Decimal(close),
        )
        for index, close in enumerate(closes)
    ]


def universe() -> list[OhlcvBar]:
    return [
        *series(
            "AAPL",
            [
                "100",
                "101",
                "102",
                "103",
                "104",
                "105",
                "106",
                "108",
                "108",
                "109",
                "110",
            ],
        ),
        *series(
            "MSFT",
            [
                "100",
                "100.5",
                "101",
                "101.5",
                "102",
                "102.5",
                "103",
                "104",
                "104.5",
                "105",
                "105.5",
            ],
        ),
        *series(
            "NVDA",
            ["107", "106", "105", "104", "103", "102", "101", "100", "99", "98", "97"],
        ),
    ]


def portfolio_policy() -> CrossSectionalPortfolioPolicy:
    return CrossSectionalPortfolioPolicy(
        opening_cash=Decimal("10000"),
        fee_per_fill=Decimal("0.50"),
        slippage_bps=Decimal("5"),
        maximum_gross_exposure_fraction=Decimal("0.60"),
        new_position_target_equity_fraction=Decimal("0.29"),
    )


def target_planner() -> CrossSectionalTargetPlanner:
    return CrossSectionalTargetPlanner(
        selector=CrossSectionalSelector(top_k=2),
        portfolio_policy=portfolio_policy(),
        position_policy=PositionManagementPolicy(),
    )


def paper_risk() -> PreTradeRiskEngine:
    return PreTradeRiskEngine(
        RiskLimits(
            maximum_order_notional=Decimal("4000"),
            maximum_symbol_notional=Decimal("4000"),
            maximum_gross_notional=Decimal("6000"),
        )
    )


def reference_prices(*, tsla: bool = False) -> dict[str, Decimal]:
    prices = {
        "AAPL": Decimal("110"),
        "MSFT": Decimal("105.5"),
        "NVDA": Decimal("97"),
    }
    if tsla:
        prices["TSLA"] = Decimal("200")
    return prices


def seed_long(
    ledger: PortfolioLedger,
    *,
    symbol: str,
    quantity: Decimal,
    price: Decimal,
) -> None:
    ledger.apply_fill(
        Fill(
            fill_id=f"seed-{symbol}",
            order_intent_id=f"seed-intent-{symbol}",
            symbol=symbol,
            side=Side.BUY,
            quantity=quantity,
            price=price,
            occurred_at=START,
        )
    )


def test_cross_sectional_targets_flow_into_conservative_paper_risk_plan() -> None:
    ledger = PortfolioLedger(opening_cash=Decimal("10000"))
    generated_at = START + timedelta(days=10, minutes=1)
    target_plan = target_planner().plan(
        universe(),
        ledger=ledger,
        reference_prices=reference_prices(),
        generated_at=generated_at,
    )

    assert target_plan.selected_symbols == ("AAPL", "MSFT")
    assert [target.symbol for target in target_plan.targets] == ["AAPL", "MSFT"]
    assert target_plan.entry_blocks == ()
    assert target_plan.reserved_entry_notional == Decimal("5800")
    assert all(
        target.quantity * target.reference_price == Decimal("2900")
        for target in target_plan.targets
    )

    paper_plan = PortfolioPaperPlanner(
        ledger=ledger,
        risk=paper_risk(),
    ).plan(
        target_plan.targets,
        mark_prices=reference_prices(),
    )
    assert paper_plan.approved_entry_count == 2
    assert paper_plan.approved_exit_count == 0
    assert paper_plan.reserved_buy_notional == Decimal("5800")


def test_protective_exit_wins_over_selected_hold_and_prevents_same_cycle_reentry() -> None:
    ledger = PortfolioLedger(opening_cash=Decimal("10000"))
    seed_long(
        ledger,
        symbol="AAPL",
        quantity=Decimal("20"),
        price=Decimal("100"),
    )
    generated_at = START + timedelta(days=10, minutes=1)
    plan = target_planner().plan(
        universe(),
        ledger=ledger,
        reference_prices=reference_prices(),
        generated_at=generated_at,
        protective_exits={"AAPL": PortfolioExitReason.INTRABAR_PROFIT_PROTECTION},
    )

    assert plan.selected_symbols == ("AAPL", "MSFT")
    assert [target.symbol for target in plan.targets] == ["AAPL", "MSFT"]
    assert plan.targets[0].quantity == 0
    assert plan.exit_reasons == (
        ("AAPL", PortfolioExitReason.INTRABAR_PROFIT_PROTECTION),
    )
    assert sum(target.symbol == "AAPL" for target in plan.targets) == 1


def test_reentry_confirmation_block_is_preserved_in_target_decision() -> None:
    ledger = PortfolioLedger(opening_cash=Decimal("10000"))
    generated_at = START + timedelta(days=10, minutes=1)
    plan = target_planner().plan(
        universe(),
        ledger=ledger,
        reference_prices=reference_prices(),
        generated_at=generated_at,
        blocked_entries={
            "AAPL": PortfolioEntryBlockReason.REENTRY_CONFIRMATION_PENDING
        },
    )

    assert plan.entry_blocks == (
        ("AAPL", PortfolioEntryBlockReason.REENTRY_CONFIRMATION_PENDING),
    )
    assert [target.symbol for target in plan.targets] == ["MSFT"]
    assert plan.reserved_entry_notional == Decimal("2900")


def test_planned_exit_does_not_create_same_cycle_strategy_gross_capacity() -> None:
    ledger = PortfolioLedger(opening_cash=Decimal("10000"))
    seed_long(
        ledger,
        symbol="AAPL",
        quantity=Decimal("58"),
        price=Decimal("100"),
    )
    prices = reference_prices()
    prices["AAPL"] = Decimal("100")
    generated_at = START + timedelta(days=10, minutes=1)
    plan = target_planner().plan(
        universe(),
        ledger=ledger,
        reference_prices=prices,
        generated_at=generated_at,
        protective_exits={"AAPL": PortfolioExitReason.INTRABAR_PROFIT_PROTECTION},
    )

    assert plan.starting_gross_notional == Decimal("5800")
    assert plan.gross_admission_cap == Decimal("6000")
    assert plan.targets == (plan.targets[0],)
    assert plan.targets[0].symbol == "AAPL" and plan.targets[0].quantity == 0
    assert plan.entry_blocks == (
        ("MSFT", PortfolioEntryBlockReason.GROSS_EXPOSURE_CAP),
    )
    assert plan.reserved_entry_notional == 0


def test_unmanaged_position_is_observed_but_not_liquidated_by_strategy() -> None:
    ledger = PortfolioLedger(opening_cash=Decimal("10000"))
    seed_long(
        ledger,
        symbol="TSLA",
        quantity=Decimal("1"),
        price=Decimal("200"),
    )
    generated_at = START + timedelta(days=10, minutes=1)
    plan = target_planner().plan(
        universe(),
        ledger=ledger,
        reference_prices=reference_prices(tsla=True),
        generated_at=generated_at,
    )

    assert plan.unmanaged_position_symbols == ("TSLA",)
    assert all(target.symbol != "TSLA" for target in plan.targets)
