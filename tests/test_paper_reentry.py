from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.application.cross_sectional_target_planner import CrossSectionalTargetPlanner
from app.application.paper_reentry import (
    PaperReentryController,
    SQLitePaperReentryStore,
)
from app.portfolio.ledger import PortfolioLedger
from app.strategy.cross_sectional_portfolio import (
    CrossSectionalPortfolioPolicy,
    PortfolioEntryBlockReason,
)
from app.strategy.cross_sectional_selection import (
    CrossSectionalSelection,
    SelectionCandidate,
)
from app.strategy.position_management import PositionManagementPolicy
from app.strategy.reentry_confirmation import ReentryConfirmationPolicy

NOW = datetime(2026, 8, 11, 22, 0, tzinfo=UTC)


def controller(tmp_path) -> PaperReentryController:
    return PaperReentryController(
        store=SQLitePaperReentryStore(tmp_path / "paper-reentry.sqlite"),
        policy=ReentryConfirmationPolicy(minimum_consecutive_eligible_bars=2),
    )


def selection(
    decision_time: datetime,
    *selected_symbols: str,
) -> CrossSectionalSelection:
    return CrossSectionalSelection(
        decision_time=decision_time,
        selected_symbols=tuple(selected_symbols),
        candidates=(),
    )


def test_reentry_confirmation_survives_restart_and_deduplicates_signal_bar(
    tmp_path,
) -> None:
    first = controller(tmp_path)
    armed = first.record_exit_fill(
        event_id="exit-fill-1",
        symbol="AAPL",
        occurred_at=NOW,
    )
    assert armed.applied
    assert armed.state is not None
    assert armed.state.consecutive_selected_decisions == 0

    first_signal_time = NOW + timedelta(minutes=1)
    blocked = first.evaluate_selection(selection(first_signal_time, "AAPL"))
    assert len(blocked) == 1
    assert blocked[0].allow_entry is False
    assert blocked[0].reason is PortfolioEntryBlockReason.REENTRY_CONFIRMATION_PENDING
    assert blocked[0].confirmation_streak == 1

    duplicate = first.evaluate_selection(selection(first_signal_time, "AAPL"))
    assert duplicate[0].confirmation_streak == 1
    assert duplicate[0].allow_entry is False

    restarted = controller(tmp_path)
    confirmed = restarted.evaluate_selection(
        selection(first_signal_time + timedelta(minutes=1), "AAPL")
    )
    assert confirmed[0].confirmation_streak == 2
    assert confirmed[0].allow_entry is True
    assert confirmed[0].reason is None
    persisted = restarted.store.state(
        strategy_id=restarted.strategy_id,
        symbol="AAPL",
    )
    assert persisted is not None
    assert persisted.consecutive_selected_decisions == 2


def test_reentry_state_clears_only_on_entry_fill_and_old_exit_replay_is_harmless(
    tmp_path,
) -> None:
    gate = controller(tmp_path)
    gate.record_exit_fill(
        event_id="exit-fill-1",
        symbol="AAPL",
        occurred_at=NOW,
    )
    gate.evaluate_selection(selection(NOW + timedelta(minutes=1), "AAPL"))
    gate.evaluate_selection(selection(NOW + timedelta(minutes=2), "AAPL"))
    assert gate.store.state(strategy_id=gate.strategy_id, symbol="AAPL") is not None

    cleared = gate.record_entry_fill(
        event_id="entry-fill-1",
        symbol="AAPL",
        occurred_at=NOW + timedelta(minutes=3),
    )
    assert cleared.applied
    assert cleared.state is None

    replay = gate.record_exit_fill(
        event_id="exit-fill-1",
        symbol="AAPL",
        occurred_at=NOW,
    )
    assert replay.applied is False
    assert replay.state is None
    assert gate.store.state(strategy_id=gate.strategy_id, symbol="AAPL") is None


def test_unselected_decision_resets_reentry_confirmation_streak(tmp_path) -> None:
    gate = controller(tmp_path)
    gate.record_exit_fill(
        event_id="exit-fill-reset",
        symbol="AAPL",
        occurred_at=NOW,
    )
    first = gate.evaluate_selection(selection(NOW + timedelta(minutes=1), "AAPL"))
    assert first[0].confirmation_streak == 1

    reset = gate.evaluate_selection(selection(NOW + timedelta(minutes=2), "MSFT"))
    assert reset[0].selected is False
    assert reset[0].confirmation_streak == 0
    assert reset[0].reason is None

    blocked_again = gate.evaluate_selection(
        selection(NOW + timedelta(minutes=3), "AAPL")
    )
    assert blocked_again[0].confirmation_streak == 1
    assert blocked_again[0].allow_entry is False


def test_stale_reentry_signal_decision_fails_closed(tmp_path) -> None:
    gate = controller(tmp_path)
    gate.record_exit_fill(
        event_id="exit-fill-stale",
        symbol="AAPL",
        occurred_at=NOW,
    )
    gate.evaluate_selection(selection(NOW + timedelta(minutes=2), "AAPL"))

    with pytest.raises(ValueError, match="stale paper re-entry decision"):
        gate.evaluate_selection(selection(NOW + timedelta(minutes=1), "AAPL"))


def candidate(symbol: str, rank: int) -> SelectionCandidate:
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

    def __init__(self, decision_time: datetime) -> None:
        self.decision_time = decision_time

    def select(self, bars) -> CrossSectionalSelection:
        del bars
        return CrossSectionalSelection(
            decision_time=self.decision_time,
            selected_symbols=("AAPL", "MSFT"),
            candidates=(candidate("AAPL", 1), candidate("MSFT", 2)),
        )


def test_target_planner_consumes_durable_reentry_blocks_without_double_counting(
    tmp_path,
) -> None:
    gate = controller(tmp_path)
    gate.record_exit_fill(
        event_id="exit-fill-integrated",
        symbol="AAPL",
        occurred_at=NOW,
    )
    selector = FakeSelector(NOW + timedelta(minutes=1))
    planner = CrossSectionalTargetPlanner(
        selector=selector,
        portfolio_policy=CrossSectionalPortfolioPolicy(
            opening_cash=Decimal("10000"),
            maximum_gross_exposure_fraction=Decimal("0.60"),
            new_position_target_equity_fraction=Decimal("0.29"),
        ),
        position_policy=PositionManagementPolicy(),
        entry_block_provider=gate,
    )
    ledger = PortfolioLedger(opening_cash=Decimal("10000"))
    prices = {"AAPL": Decimal("100"), "MSFT": Decimal("100")}

    first = planner.plan(
        (),
        ledger=ledger,
        reference_prices=prices,
        generated_at=selector.decision_time + timedelta(seconds=1),
    )
    assert first.entry_blocks == (
        ("AAPL", PortfolioEntryBlockReason.REENTRY_CONFIRMATION_PENDING),
    )
    assert [target.symbol for target in first.targets] == ["MSFT"]

    duplicate = planner.plan(
        (),
        ledger=ledger,
        reference_prices=prices,
        generated_at=selector.decision_time + timedelta(seconds=2),
    )
    assert duplicate.entry_blocks == first.entry_blocks
    state = gate.store.state(strategy_id=gate.strategy_id, symbol="AAPL")
    assert state is not None and state.consecutive_selected_decisions == 1

    selector.decision_time += timedelta(minutes=1)
    confirmed = planner.plan(
        (),
        ledger=ledger,
        reference_prices=prices,
        generated_at=selector.decision_time + timedelta(seconds=1),
    )
    assert confirmed.entry_blocks == ()
    assert [target.symbol for target in confirmed.targets] == ["AAPL", "MSFT"]
