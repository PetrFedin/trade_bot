from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.application.paper_protection import (
    PaperProtectionService,
    PaperProtectionStatus,
    SQLitePaperProtectionStore,
)
from app.domain.trading import Fill, Side
from app.portfolio.ledger import PortfolioLedger
from app.strategy.position_management import ExitReason, PositionManagementPolicy

NOW = datetime(2026, 8, 11, 21, 0, tzinfo=UTC)


def seed_long(
    ledger: PortfolioLedger,
    *,
    quantity: str = "1",
    price: str = "100",
) -> None:
    ledger.apply_fill(
        Fill(
            fill_id="seed-aapl",
            order_intent_id="seed-intent-aapl",
            symbol="AAPL",
            side=Side.BUY,
            quantity=Decimal(quantity),
            price=Decimal(price),
            occurred_at=NOW - timedelta(minutes=1),
        )
    )


def profit_policy() -> PositionManagementPolicy:
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


def service(
    tmp_path,
    ledger: PortfolioLedger,
    *,
    policy: PositionManagementPolicy | None = None,
) -> PaperProtectionService:
    return PaperProtectionService(
        ledger=ledger,
        store=SQLitePaperProtectionStore(tmp_path / "paper-protection.sqlite"),
        policy=profit_policy() if policy is None else policy,
    )


def test_profit_peak_persists_across_restart_and_triggers_protected_exit(tmp_path) -> None:
    ledger = PortfolioLedger(opening_cash=Decimal("1000"))
    seed_long(ledger)
    first_service = service(tmp_path, ledger)

    peak = first_service.observe(
        symbol="AAPL",
        reference_price=Decimal("102"),
        observed_at=NOW,
    )
    assert peak.status is PaperProtectionStatus.TRACKING
    assert peak.state is not None
    assert peak.state.peak_reference_price == Decimal("102")
    assert peak.protected_stop_price == Decimal("101.00")

    restarted = service(tmp_path, ledger)
    triggered = restarted.observe(
        symbol="AAPL",
        reference_price=Decimal("101"),
        observed_at=NOW + timedelta(seconds=1),
    )
    assert triggered.status is PaperProtectionStatus.EXIT_PENDING
    assert triggered.exit_reason is ExitReason.PROFIT_PROTECTION
    assert triggered.exit_target is not None
    assert triggered.exit_target.quantity == 0
    assert triggered.exit_target.reference_price == Decimal("101")
    assert triggered.trigger_quantity == Decimal("1")

    retry = restarted.observe(
        symbol="AAPL",
        reference_price=Decimal("99"),
        observed_at=NOW + timedelta(seconds=2),
    )
    assert retry.status is PaperProtectionStatus.EXIT_PENDING
    assert retry.exit_target == triggered.exit_target
    assert retry.trigger_quantity == triggered.trigger_quantity


def test_gap_through_protection_uses_observed_price_not_unavailable_stop(tmp_path) -> None:
    ledger = PortfolioLedger(opening_cash=Decimal("1000"))
    seed_long(ledger)
    monitor = service(tmp_path, ledger)

    peak = monitor.observe(
        symbol="AAPL",
        reference_price=Decimal("106"),
        observed_at=NOW,
    )
    assert peak.status is PaperProtectionStatus.TRACKING
    assert peak.protected_stop_price == Decimal("103.00")

    gap = monitor.observe(
        symbol="AAPL",
        reference_price=Decimal("95"),
        observed_at=NOW + timedelta(seconds=1),
    )
    assert gap.status is PaperProtectionStatus.EXIT_PENDING
    assert gap.exit_reason is ExitReason.PROFIT_PROTECTION
    assert gap.protected_stop_price == Decimal("103.00")
    assert gap.exit_target is not None
    assert gap.exit_target.reference_price == Decimal("95")


def test_completed_bar_count_is_idempotent_and_drives_time_stop(tmp_path) -> None:
    ledger = PortfolioLedger(opening_cash=Decimal("1000"))
    seed_long(ledger)
    policy = PositionManagementPolicy(
        stop_loss_fraction=Decimal("0.20"),
        take_profit_fraction=Decimal("0.50"),
        trailing_activation_fraction=Decimal("0.40"),
        trailing_stop_fraction=Decimal("0.10"),
        maximum_holding_bars=2,
    )
    monitor = service(tmp_path, ledger, policy=policy)
    first_bar = NOW - timedelta(seconds=1)

    first = monitor.observe(
        symbol="AAPL",
        reference_price=Decimal("100"),
        observed_at=NOW,
        completed_bar_at=first_bar,
    )
    assert first.status is PaperProtectionStatus.TRACKING
    assert first.state is not None and first.state.completed_bars_held == 1

    duplicate = monitor.observe(
        symbol="AAPL",
        reference_price=Decimal("100"),
        observed_at=NOW + timedelta(seconds=1),
        completed_bar_at=first_bar,
    )
    assert duplicate.status is PaperProtectionStatus.TRACKING
    assert duplicate.state is not None and duplicate.state.completed_bars_held == 1

    second = monitor.observe(
        symbol="AAPL",
        reference_price=Decimal("100"),
        observed_at=NOW + timedelta(seconds=2),
        completed_bar_at=NOW + timedelta(seconds=1),
    )
    assert second.status is PaperProtectionStatus.EXIT_PENDING
    assert second.exit_reason is ExitReason.TIME_STOP
    assert second.state is not None and second.state.completed_bars_held == 2


def test_flat_ledger_clears_pending_protection_state(tmp_path) -> None:
    ledger = PortfolioLedger(opening_cash=Decimal("1000"))
    seed_long(ledger)
    monitor = service(tmp_path, ledger)
    monitor.observe(
        symbol="AAPL",
        reference_price=Decimal("102"),
        observed_at=NOW,
    )
    pending = monitor.observe(
        symbol="AAPL",
        reference_price=Decimal("101"),
        observed_at=NOW + timedelta(seconds=1),
    )
    assert pending.status is PaperProtectionStatus.EXIT_PENDING

    ledger.apply_fill(
        Fill(
            fill_id="exit-aapl",
            order_intent_id="exit-intent-aapl",
            symbol="AAPL",
            side=Side.SELL,
            quantity=Decimal("1"),
            price=Decimal("100"),
            occurred_at=NOW + timedelta(seconds=2),
        )
    )
    flat = monitor.observe(
        symbol="AAPL",
        reference_price=Decimal("100"),
        observed_at=NOW + timedelta(seconds=3),
    )
    assert flat.status is PaperProtectionStatus.FLAT
    assert flat.state is None
    assert monitor.store.get(
        strategy_id=monitor.strategy_id,
        symbol="AAPL",
    ) is None
