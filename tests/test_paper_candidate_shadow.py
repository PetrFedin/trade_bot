from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from app.application.paper_candidate_shadow import (
    EntryQualityPaperShadowObserver,
    PaperCandidateKind,
    PaperCandidateShadowSuite,
    SQLitePaperCandidateShadowStore,
    SelectionExitPaperShadowObserver,
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


def test_entry_quality_shadow_records_candidate_divergence_idempotently(
    tmp_path: Path,
) -> None:
    universe = tuple(
        bars(
            "MSFT",
            ["100", "100.5", "101", "101.5", "102", "102.5", "103", "112"],
        )
        + bars("AAPL", ["100", "101", "102", "103", "104", "105", "106", "107"])
        + bars("NVDA", ["90", "91", "92", "93", "94", "95", "96", "97"])
    )
    decision_time = max(bar.timestamp for bar in universe)
    store = SQLitePaperCandidateShadowStore(tmp_path / "candidate.sqlite")
    observer = EntryQualityPaperShadowObserver(
        strategy_id=STRATEGY,
        selector=EntryQualityFilteredSelector(
            base_selector=FakeBaseSelector(),
            policy=EntryQualityPolicy(
                lookback_bars=8,
                minimum_trend_efficiency=Decimal("0.35"),
                maximum_price_extension_fraction=Decimal("0.04"),
                maximum_single_bar_return_fraction=Decimal("0.05"),
            ),
        ),
        store=store,
    )
    baseline = plan(
        decision_time=decision_time,
        selected=("MSFT", "AAPL"),
    )

    first = observer.observe(
        universe,
        baseline_plan=baseline,
        observed_at=decision_time + timedelta(seconds=1),
    )
    replay = observer.observe(
        universe,
        baseline_plan=baseline,
        observed_at=decision_time + timedelta(seconds=5),
    )

    assert replay == first
    persisted = store.records(
        strategy_id=STRATEGY,
        candidate=PaperCandidateKind.ENTRY_QUALITY,
    )
    assert len(persisted) == 3
    msft = next(record for record in persisted if record.symbol == "MSFT")
    nvda = next(record for record in persisted if record.symbol == "NVDA")
    assert msft.baseline_action == "SELECT"
    assert msft.candidate_action == "SKIP"
    assert "PRICE_EXTENSION_ABOVE_MAXIMUM" in msft.reasons
    assert nvda.baseline_action == "SKIP"
    assert nvda.candidate_action == "SELECT"
    assert all(record.evidence_scope == "DECISION_DIVERGENCE_ONLY" for record in persisted)


def seed_long(ledger: PortfolioLedger) -> None:
    ledger.apply_fill(
        Fill(
            fill_id="seed-aapl",
            order_intent_id="seed-aapl-intent",
            symbol="AAPL",
            side=Side.BUY,
            quantity=Decimal("1"),
            price=Decimal("100"),
            occurred_at=START,
        )
    )


def test_selection_exit_shadow_replay_does_not_double_count_completed_bar(
    tmp_path: Path,
) -> None:
    ledger = PortfolioLedger(opening_cash=Decimal("1000"))
    seed_long(ledger)
    store = SQLitePaperCandidateShadowStore(tmp_path / "candidate.sqlite")
    observer = SelectionExitPaperShadowObserver(
        strategy_id=STRATEGY,
        ledger=ledger,
        policy=SelectionExitConfirmationPolicy(
            minimum_consecutive_deselected_bars=2,
            exit_profitable_positions_immediately=True,
            reset_on_reselection=True,
        ),
        store=store,
    )
    first_bars = tuple(
        bars("AAPL", ["100", "99"]) + bars("MSFT", ["100", "101"])
    )
    first_time = START + timedelta(days=1)
    first_plan = plan(
        decision_time=first_time,
        selected=("MSFT",),
        exit_reasons=(("AAPL", PortfolioExitReason.SELECTION_EXIT),),
    )

    first = observer.observe(
        first_bars,
        baseline_plan=first_plan,
        observed_at=first_time + timedelta(seconds=1),
    )
    replay = observer.observe(
        first_bars,
        baseline_plan=first_plan,
        observed_at=first_time + timedelta(seconds=10),
    )

    assert replay == first
    assert first[0].baseline_action == "EXIT:SELECTION_EXIT"
    assert first[0].candidate_action == "PENDING:SELECTION_EXIT"
    stored = store.selection_state(strategy_id=STRATEGY, symbol="AAPL")
    assert stored.state.consecutive_deselected_bars == 1
    assert stored.last_decision_time == first_time

    second_bars = tuple(
        bars("AAPL", ["100", "99", "98.5"])
        + bars("MSFT", ["100", "101", "102"])
    )
    second_time = START + timedelta(days=2)
    second = observer.observe(
        second_bars,
        baseline_plan=plan(
            decision_time=second_time,
            selected=("MSFT",),
            exit_reasons=(("AAPL", PortfolioExitReason.SELECTION_EXIT),),
        ),
        observed_at=second_time + timedelta(seconds=1),
    )

    assert second[0].candidate_action == "EXIT:SELECTION_EXIT"
    assert second[0].reasons == ("DESELECTION_CONFIRMED",)
    assert store.selection_state(
        strategy_id=STRATEGY,
        symbol="AAPL",
    ).state.consecutive_deselected_bars == 0


def test_selection_exit_shadow_never_delays_protective_exit(tmp_path: Path) -> None:
    ledger = PortfolioLedger(opening_cash=Decimal("1000"))
    seed_long(ledger)
    store = SQLitePaperCandidateShadowStore(tmp_path / "candidate.sqlite")
    observer = SelectionExitPaperShadowObserver(
        strategy_id=STRATEGY,
        ledger=ledger,
        policy=SelectionExitConfirmationPolicy(),
        store=store,
    )
    universe = tuple(
        bars("AAPL", ["100", "98"]) + bars("MSFT", ["100", "101"])
    )
    decision_time = START + timedelta(days=1)

    records = observer.observe(
        universe,
        baseline_plan=plan(
            decision_time=decision_time,
            selected=("MSFT",),
            exit_reasons=(("AAPL", PortfolioExitReason.INTRABAR_HARD_STOP),),
        ),
        observed_at=decision_time + timedelta(seconds=1),
    )

    assert records[0].baseline_action == "EXIT:INTRABAR_HARD_STOP"
    assert records[0].candidate_action == "EXIT:INTRABAR_HARD_STOP"
    assert records[0].reasons == ("RISK_OR_PROTECTIVE_EXIT_UNCHANGED",)


class FailingObserver:
    name = "BROKEN_SHADOW"

    def observe(self, bars, *, baseline_plan, observed_at):
        del bars, baseline_plan, observed_at
        raise RuntimeError("shadow observer exploded")


def test_shadow_suite_captures_failure_instead_of_raising() -> None:
    batch = PaperCandidateShadowSuite((FailingObserver(),)).observe(
        (),
        baseline_plan=plan(decision_time=START, selected=()),
        observed_at=START,
    )

    assert batch.records == ()
    assert batch.divergence_count == 0
    assert len(batch.failures) == 1
    assert batch.failures[0].observer == "BROKEN_SHADOW"
    assert batch.failures[0].error_type == "RuntimeError"
