from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.application.order_lifecycle import PaperOrderLifecycle
from app.application.paper_protection import (
    PaperProtectionService,
    SQLitePaperProtectionStore,
)
from app.application.paper_protection_cycle import PaperProtectionOrderService
from app.application.paper_trade_quality import (
    PaperTradeQualityTracker,
    SQLitePaperTradeQualityStore,
)
from app.application.portfolio_paper_planner import PortfolioPaperPlanner
from app.domain.trading import Fill, Side
from app.oms.store import DurableOmsStore
from app.portfolio.ledger import PortfolioLedger
from app.risk.pretrade import PreTradeRiskEngine, RiskLimits
from app.strategy.position_management import PositionManagementPolicy

NOW = datetime(2026, 8, 11, 23, 30, tzinfo=UTC)


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


def risk_engine() -> PreTradeRiskEngine:
    return PreTradeRiskEngine(
        RiskLimits(
            maximum_order_notional=Decimal("1000"),
            maximum_symbol_notional=Decimal("2000"),
            maximum_gross_notional=Decimal("5000"),
        )
    )


def test_protection_cycle_records_fresh_price_and_exit_reason_before_outbox(
    tmp_path: Path,
) -> None:
    ledger = PortfolioLedger(opening_cash=Decimal("1000"))
    entry = Fill(
        fill_id="entry-fill",
        order_intent_id="entry-intent",
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("1"),
        price=Decimal("100"),
        occurred_at=NOW - timedelta(seconds=1),
    )
    ledger.apply_fill(entry)
    quality = PaperTradeQualityTracker(
        store=SQLitePaperTradeQualityStore(tmp_path / "quality.sqlite")
    )
    quality.observe_fill(entry)
    protection = PaperProtectionService(
        ledger=ledger,
        store=SQLitePaperProtectionStore(tmp_path / "protection.sqlite"),
        policy=protection_policy(),
    )
    lifecycle = PaperOrderLifecycle(DurableOmsStore(tmp_path / "oms.sqlite"))
    service = PaperProtectionOrderService(
        protection=protection,
        planner=PortfolioPaperPlanner(ledger=ledger, risk=risk_engine()),
        lifecycle=lifecycle,
        quality_recorder=quality,
    )

    armed = service.observe_and_prepare(
        symbol="AAPL",
        reference_price=Decimal("102"),
        observed_at=NOW,
        mark_prices={"AAPL": Decimal("102")},
    )
    assert armed.prepared is None
    tracked = quality.store.open_trade(
        strategy_id=quality.strategy_id,
        symbol="AAPL",
    )
    assert tracked is not None
    assert tracked.peak_reference_price == Decimal("102")

    triggered = service.observe_and_prepare(
        symbol="AAPL",
        reference_price=Decimal("101"),
        observed_at=NOW + timedelta(seconds=1),
        mark_prices={"AAPL": Decimal("101")},
    )
    assert triggered.prepared is not None
    assert triggered.plan_item is not None
    assert triggered.plan_item.intent is not None
    assert len(lifecycle.store.pending_outbox()) == 1

    quality.observe_fill(
        Fill(
            fill_id="exit-fill",
            order_intent_id=triggered.plan_item.intent.intent_id,
            symbol="AAPL",
            side=Side.SELL,
            quantity=Decimal("1"),
            price=Decimal("101"),
            occurred_at=NOW + timedelta(seconds=2),
        )
    )
    closed = quality.store.closed_trades(strategy_id=quality.strategy_id)
    assert len(closed) == 1
    assert closed[0].exit_reason == "PROFIT_PROTECTION"
    assert closed[0].net_pnl == Decimal("1")
    assert closed[0].maximum_favorable_excursion_fraction == Decimal("0.02")
    assert closed[0].mfe_capture_ratio == Decimal("0.5")
