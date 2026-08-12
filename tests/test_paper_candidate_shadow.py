from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from app.application.paper_candidate_shadow import (
    EntryQualityPaperShadowObserver,
    PaperCandidateShadowSuite,
    SelectionExitPaperShadowObserver,
    SQLitePaperCandidateShadowStore,
)
from app.domain.trading import Fill, Side
from app.marketdata.ohlcv import OhlcvBar
from app.portfolio.ledger import PortfolioLedger
from app.strategy.cross_sectional_portfolio import PortfolioExitReason
from app.strategy.cross_sectional_selection import (
    CrossSectionalSelection,
    SelectionCandidate,
)
from app.strategy.entry_quality import EntryQualityFilteredSelector, EntryQualityPolicy
from app.strategy.selection_exit_confirmation import SelectionExitConfirmationPolicy

START = datetime(2026, 8, 1, tzinfo=UTC)
STRATEGY = "cross-sectional-quality-v2-paper-shadow"


def bars(symbol: str, closes: list[str]) -> list[OhlcvBar]:
    result = []
    for index, close_text in enumerate(closes):
        close = Decimal(close_text)
        result.append(
            OhlcvBar(
                symbol=symbol,
                timestamp=START + timedelta(days=index),
                open=close,
                high=close + Decimal("0.5"),
                low=close - Decimal("0.5"),
                close=close,
                volume=1_000_000,
                trade_count=10_000,
                vwap=close,
            )
        )
    return result


def candidate(symbol: str, rank: int) -> SelectionCandidate:
    return SelectionCandidate(
        rank=rank,
        symbol=symbol,
        eligible=True,
        rejection_reasons=(),
        momentum_return=Decimal("0.05"),
        trend_strength=Decimal("0.03"),
        realized_volatility=Decimal("0.01"),
        quality_score=Decimal("0.07"),
        reference_price=Decimal("100"),
    )


class FakeBaseSelector:
    top_k = 2

    def select(self, materialized) -> CrossSectionalSelection:
        decision_time = max(bar.timestamp for bar in materialized)
        return CrossSectionalSelection(
            decision_time=decision_time,
            selected_symbols=("MSFT", "AAPL"),
            candidates=(
                candidate("MSFT", 1),
                candidate("AAPL", 2),
                candidate("NVDA", 3),
            ),
        )


def plan(*, decision_time: datetime, selected, exit_reasons=()):
    return SimpleNamespace(
        decision_time=decision_time,
        selected_symbols=tuple(selected),
        exit_reasons=tuple(exit_reasons),
    )


def ledger_with_position(tmp_path: Path, symbol: str = "AAPL") -> PortfolioLedger:
    del tmp_path
    ledger = PortfolioLedger(opening_cash=Decimal("10000"))
    ledger.apply_fill(
        Fill(
            fill_id=f"seed-{symbol}",
            order_intent_id=f"seed-intent-{symbol}",
            symbol=symbol,
            side=Side.BUY,
            quantity=Decimal("10"),
            price=Decimal("100"),
            occurred_at=START,
            fee=Decimal("0"),
        )
    )
    return ledger


def test_entry_quality_shadow_records_late_spike_divergence(tmp_path: Path) -> None:
    materialized = tuple(
        bars("MSFT", ["100", "101", "102", "103", "104", "105", "106", "107"])
        + bars("AAPL", ["100", "100.5", "101", "101.5", "102", "102.5", "103", "104"])
        + bars("NVDA", ["100", "100.5", "101", "101.5", "102", "102.5", "103", "110"])
    )
    selector = EntryQualityFilteredSelector(
        base_selector=FakeBaseSelector(),
        policy=EntryQualityPolicy(
            lookback_bars=8,
            minimum_trend_efficiency=Decimal("0.35"),
            maximum_price_extension_fraction=Decimal("0.04"),
            maximum_single_bar_return_fraction=Decimal("0.05"),
        ),
    )
    store = SQLitePaperCandidateShadowStore(tmp_path / "shadow.sqlite")
    observer = EntryQualityPaperShadowObserver(
        strategy_id=STRATEGY,
        selector=selector,
        store=store,
    )
    baseline = plan(
        decision_time=max(bar.timestamp for bar in materialized),
        selected=("MSFT", "AAPL"),
    )

    records = observer.observe(
        materialized,
        baseline_plan=baseline,
        observed_at=START + timedelta(days=8),
    )

    assert records
    nvda = next(record for record in records if record.symbol == "NVDA")
    assert nvda.baseline_action == "SKIP"
    assert nvda.candidate_action == "SKIP"
    assert "SINGLE_BAR_RETURN_ABOVE_MAXIMUM" in nvda.reasons
    assert all(record.evidence_scope == "DECISION_DIVERGENCE_ONLY" for record in records)


def test_selection_exit_shadow_replay_does_not_double_increment_streak(
    tmp_path: Path,
) -> None:
    materialized = tuple(
        bars("AAPL", ["100", "100", "100", "100", "100", "100", "100", "99"])
        + bars("MSFT", ["100", "101", "102", "103", "104", "105", "106", "107"])
    )
    store = SQLitePaperCandidateShadowStore(tmp_path / "shadow.sqlite")
    ledger = ledger_with_position(tmp_path)
    observer = SelectionExitPaperShadowObserver(
        strategy_id=STRATEGY,
        ledger=ledger,
        policy=SelectionExitConfirmationPolicy(
            minimum_consecutive_deselected_bars=2,
            immediate_exit_when_profitable=True,
        ),
        store=store,
    )
    decision_time = max(bar.timestamp for bar in materialized)
    baseline = plan(
        decision_time=decision_time,
        selected=("MSFT",),
        exit_reasons=(("AAPL", PortfolioExitReason.SELECTION_EXIT),),
    )

    first = observer.observe(
        materialized,
        baseline_plan=baseline,
        observed_at=decision_time + timedelta(hours=1),
    )
    second = observer.observe(
        materialized,
        baseline_plan=baseline,
        observed_at=decision_time + timedelta(hours=1),
    )

    assert first == second
    record = next(item for item in first if item.symbol == "AAPL")
    assert record.candidate_action == "PENDING:SELECTION_EXIT"
    assert record.metrics["prior_deselection_streak"] == 0
    assert record.metrics["next_deselection_streak"] == 1


def test_selection_exit_shadow_never_delays_protective_exit(tmp_path: Path) -> None:
    materialized = tuple(
        bars("AAPL", ["100", "101", "102", "103", "104", "105", "106", "107"])
        + bars("MSFT", ["100", "101", "102", "103", "104", "105", "106", "107"])
    )
    store = SQLitePaperCandidateShadowStore(tmp_path / "shadow.sqlite")
    ledger = ledger_with_position(tmp_path)
    observer = SelectionExitPaperShadowObserver(
        strategy_id=STRATEGY,
        ledger=ledger,
        policy=SelectionExitConfirmationPolicy(
            minimum_consecutive_deselected_bars=2,
            immediate_exit_when_profitable=True,
        ),
        store=store,
    )
    decision_time = max(bar.timestamp for bar in materialized)
    baseline = plan(
        decision_time=decision_time,
        selected=("MSFT",),
        exit_reasons=(("AAPL", PortfolioExitReason.INTRABAR_HARD_STOP),),
    )

    records = observer.observe(
        materialized,
        baseline_plan=baseline,
        observed_at=decision_time + timedelta(hours=1),
    )

    record = next(item for item in records if item.symbol == "AAPL")
    assert record.baseline_action == "EXIT:INTRABAR_HARD_STOP"
    assert record.candidate_action == "EXIT:INTRABAR_HARD_STOP"
    assert record.reasons == ("RISK_OR_PROTECTIVE_EXIT_UNCHANGED",)


def test_suite_isolates_observer_failure() -> None:
    class BrokenObserver:
        name = "BROKEN"

        def observe(self, bars, *, baseline_plan, observed_at):
            raise RuntimeError("boom")

    batch = PaperCandidateShadowSuite((BrokenObserver(),)).observe(
        (),
        baseline_plan=plan(decision_time=START, selected=()),
        observed_at=START,
    )

    assert batch.records == ()
    assert len(batch.failures) == 1
    assert batch.failures[0].observer == "BROKEN"
    assert batch.failures[0].error_type == "RuntimeError"
