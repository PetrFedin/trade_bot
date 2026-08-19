from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.application.paper_trade_quality import (
    UNATTRIBUTED_EXIT_REASON,
    PaperTradeQualityTracker,
    SQLitePaperTradeQualityStore,
)
from app.domain.trading import Fill, Side
from app.strategy.quality_monitor import (
    StrategyQualityStatus,
    TradeQualityMonitorPolicy,
)

NOW = datetime(2026, 8, 11, 23, 0, tzinfo=UTC)


def tracker(tmp_path: Path) -> PaperTradeQualityTracker:
    return PaperTradeQualityTracker(
        store=SQLitePaperTradeQualityStore(tmp_path / "paper-quality.sqlite")
    )


def fill(
    *,
    fill_id: str,
    intent_id: str,
    side: Side,
    quantity: str,
    price: str,
    occurred_at: datetime,
    fee: str = "0",
) -> Fill:
    return Fill(
        fill_id=fill_id,
        order_intent_id=intent_id,
        symbol="AAPL",
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        fee=Decimal(fee),
        occurred_at=occurred_at,
    )


def gate_policy() -> TradeQualityMonitorPolicy:
    return TradeQualityMonitorPolicy(
        window_trades=20,
        minimum_observations=1,
        minimum_profit_factor=Decimal("1"),
        minimum_profit_preservation_rate=Decimal("0.5"),
        minimum_average_mfe_capture_ratio=Decimal("0.1"),
        maximum_hard_stop_fraction=Decimal("0.5"),
        maximum_consecutive_losses=4,
        allow_entries_when_insufficient_data=False,
    )


def test_closed_trade_uses_exact_cash_pnl_and_observed_mfe_mae(tmp_path: Path) -> None:
    quality = tracker(tmp_path)
    quality.observe_fill(
        fill(
            fill_id="buy-1",
            intent_id="entry-1",
            side=Side.BUY,
            quantity="2",
            price="100",
            fee="1",
            occurred_at=NOW,
        )
    )
    quality.observe_price(
        symbol="AAPL",
        reference_price=Decimal("110"),
        observed_at=NOW + timedelta(seconds=1),
    )
    quality.observe_price(
        symbol="AAPL",
        reference_price=Decimal("95"),
        observed_at=NOW + timedelta(seconds=2),
    )
    quality.register_exit_intent(
        intent_id="exit-1",
        symbol="AAPL",
        exit_reason="PROFIT_PROTECTION",
        registered_at=NOW + timedelta(seconds=3),
    )
    quality.observe_fill(
        fill(
            fill_id="sell-1",
            intent_id="exit-1",
            side=Side.SELL,
            quantity="2",
            price="108",
            fee="1",
            occurred_at=NOW + timedelta(seconds=4),
        )
    )

    closed = quality.store.closed_trades(strategy_id=quality.strategy_id)
    assert len(closed) == 1
    trade = closed[0]
    assert trade.quantity == Decimal("2")
    assert trade.average_entry_cost == Decimal("100.5")
    assert trade.average_exit_proceeds == Decimal("107.5")
    assert trade.net_pnl == Decimal("14")
    assert trade.return_fraction == Decimal("14") / Decimal("201")
    assert trade.maximum_favorable_excursion_fraction == (
        Decimal("9.5") / Decimal("100.5")
    )
    assert trade.maximum_adverse_excursion_fraction == (
        Decimal("5.5") / Decimal("100.5")
    )
    assert trade.mfe_capture_ratio == Decimal("14") / Decimal("19")
    assert trade.mfe_giveback_fraction == Decimal("5") / Decimal("19")
    assert trade.exit_reason == "PROFIT_PROTECTION"

    gate = quality.quality_gate(policy=gate_policy())
    assert gate.status is StrategyQualityStatus.HEALTHY
    assert gate.allow_new_entries is True
    assert gate.allow_exits is True
    assert gate.metrics.observation_count == 1
    assert gate.metrics.profit_preservation_rate == Decimal("1")


def test_late_earlier_partial_buy_rebuilds_episode_in_broker_time_order(
    tmp_path: Path,
) -> None:
    quality = tracker(tmp_path)
    later = fill(
        fill_id="buy-later",
        intent_id="entry-1",
        side=Side.BUY,
        quantity="1",
        price="102",
        occurred_at=NOW + timedelta(seconds=2),
    )
    quality.observe_fill(later)
    quality.observe_price(
        symbol="AAPL",
        reference_price=Decimal("110"),
        observed_at=NOW + timedelta(seconds=3),
    )
    earlier = fill(
        fill_id="buy-earlier",
        intent_id="entry-1",
        side=Side.BUY,
        quantity="1",
        price="100",
        occurred_at=NOW + timedelta(seconds=1),
    )
    quality.observe_fill(earlier)

    state = quality.store.open_trade(
        strategy_id=quality.strategy_id,
        symbol="AAPL",
    )
    assert state is not None
    assert state.episode_id == "buy-earlier"
    assert state.opened_at == NOW + timedelta(seconds=1)
    assert state.purchased_quantity == Decimal("2")
    assert state.open_quantity == Decimal("2")
    assert state.entry_cash_out == Decimal("202")
    assert state.peak_reference_price == Decimal("110")
    assert state.trough_reference_price == Decimal("100")

    quality.observe_fill(earlier)
    duplicate_state = quality.store.open_trade(
        strategy_id=quality.strategy_id,
        symbol="AAPL",
    )
    assert duplicate_state == state


def test_replacement_exit_intent_uses_final_fill_reason(tmp_path: Path) -> None:
    quality = tracker(tmp_path)
    quality.observe_fill(
        fill(
            fill_id="buy-1",
            intent_id="entry-1",
            side=Side.BUY,
            quantity="2",
            price="100",
            occurred_at=NOW,
        )
    )
    quality.register_exit_intent(
        intent_id="exit-old",
        symbol="AAPL",
        exit_reason="HARD_STOP",
        registered_at=NOW + timedelta(seconds=1),
    )
    quality.observe_fill(
        fill(
            fill_id="sell-partial",
            intent_id="exit-old",
            side=Side.SELL,
            quantity="1",
            price="99",
            occurred_at=NOW + timedelta(seconds=2),
        )
    )
    quality.register_exit_intent(
        intent_id="exit-replacement",
        symbol="AAPL",
        exit_reason="PROFIT_PROTECTION",
        registered_at=NOW + timedelta(seconds=3),
    )
    quality.observe_fill(
        fill(
            fill_id="sell-final",
            intent_id="exit-replacement",
            side=Side.SELL,
            quantity="1",
            price="101",
            occurred_at=NOW + timedelta(seconds=4),
        )
    )

    closed = quality.store.closed_trades(strategy_id=quality.strategy_id)
    assert len(closed) == 1
    assert closed[0].exit_intent_id == "exit-replacement"
    assert closed[0].exit_reason == "PROFIT_PROTECTION"
    assert closed[0].net_pnl == 0


def test_late_exit_reason_registration_repairs_closed_trade(tmp_path: Path) -> None:
    quality = tracker(tmp_path)
    quality.observe_fill(
        fill(
            fill_id="buy-1",
            intent_id="entry-1",
            side=Side.BUY,
            quantity="1",
            price="100",
            occurred_at=NOW,
        )
    )
    quality.observe_fill(
        fill(
            fill_id="sell-1",
            intent_id="exit-late-reason",
            side=Side.SELL,
            quantity="1",
            price="101",
            occurred_at=NOW + timedelta(seconds=1),
        )
    )
    before = quality.store.closed_trades(strategy_id=quality.strategy_id)
    assert before[0].exit_reason == UNATTRIBUTED_EXIT_REASON

    quality.register_exit_intent(
        intent_id="exit-late-reason",
        symbol="AAPL",
        exit_reason="SELECTION_EXIT",
        registered_at=NOW + timedelta(seconds=2),
    )
    after = quality.store.closed_trades(strategy_id=quality.strategy_id)
    assert after[0].episode_id == before[0].episode_id
    assert after[0].exit_reason == "SELECTION_EXIT"


def test_sparse_observation_gate_stays_fail_closed_but_never_blocks_exits(
    tmp_path: Path,
) -> None:
    quality = tracker(tmp_path)
    policy = TradeQualityMonitorPolicy(
        window_trades=20,
        minimum_observations=10,
        minimum_profit_factor=Decimal("1"),
        minimum_profit_preservation_rate=Decimal("0.5"),
        minimum_average_mfe_capture_ratio=Decimal("0.1"),
        maximum_hard_stop_fraction=Decimal("0.5"),
        maximum_consecutive_losses=4,
        allow_entries_when_insufficient_data=False,
    )

    gate = quality.quality_gate(policy=policy)
    assert gate.status is StrategyQualityStatus.INSUFFICIENT_DATA
    assert gate.allow_new_entries is False
    assert gate.allow_exits is True
    assert gate.reasons == ("INSUFFICIENT_OBSERVATIONS",)
