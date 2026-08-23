from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.marketdata.bybit_v5 import BybitKlineAcquisition, BybitKlineBar
from app.strategy.crypto_perp import (
    CryptoPerpStrategyConfig,
    build_trade_plan,
    evaluate_crypto_signal,
)
from app.strategy.crypto_shadow_outcomes import CryptoShadowSourceCandidate
from tools.evaluate_bybit_shadow_outcomes import run_shadow_outcome_cycle

_NOW = datetime(2026, 8, 23, 12, tzinfo=UTC)
_STEP = timedelta(minutes=5)


def _bars(start: datetime, count: int) -> tuple[BybitKlineBar, ...]:
    return tuple(
        BybitKlineBar(
            symbol="BTCUSDT",
            start_time=start + index * _STEP,
            open=Decimal("99") + index,
            high=Decimal("100.4") + index,
            low=Decimal("98.6") + index,
            close=Decimal("100") + index,
            volume=Decimal("10000"),
            turnover=Decimal("2000000") + index * Decimal("1000"),
        )
        for index in range(count)
    )


def _source(history: tuple[BybitKlineBar, ...]) -> CryptoShadowSourceCandidate:
    config = CryptoPerpStrategyConfig()
    evaluation = evaluate_crypto_signal(history, config)
    assert evaluation.eligible and evaluation.signal is not None
    planned = build_trade_plan(evaluation.signal, equity_usdt=Decimal("1000"), config=config)
    assert planned.eligible and planned.plan is not None
    plan = planned.plan
    return CryptoShadowSourceCandidate(
        source_snapshot_id="c" * 64,
        evidence_rank=2,
        market_rank=6,
        qualification_state="QUALIFIED_MIXED_EVIDENCE",
        symbol="BTCUSDT",
        side=evaluation.signal.side.value,
        decision_time=evaluation.signal.decision_time,
        signal_quality_score=evaluation.signal.quality_score,
        planned_notional_usdt=plan.notional_usdt,
        risk_budget_usdt=plan.risk_budget_usdt,
        estimated_round_trip_cost_usdt=plan.estimated_round_trip_cost_usdt,
    )


def test_shadow_cycle_seeds_and_evaluates_only_completed_post_signal_bars() -> None:
    history = _bars(_NOW - 119 * _STEP, 120)
    source = _source(history)
    future = _bars(_NOW + _STEP, 3)

    class Store:
        def __init__(self) -> None:
            self.seeds = []
            self.outcomes = []

        def unseeded_sources(self, *, limit: int = 200):
            assert limit == 20
            return () if self.seeds else (source,)

        def persist_seed(self, seed):
            self.seeds.append(seed)
            return seed.seed_id

        def active_seeds(self, *, limit: int = 500):
            assert limit == 30
            return tuple(self.seeds)

        def persist_outcome(self, outcome):
            self.outcomes.append(outcome)
            return outcome.evaluation_id

    class Klines:
        def __init__(self) -> None:
            self.requests = []

        def fetch(self, request):
            self.requests.append(request)
            decision_ms = int(_NOW.timestamp() * 1000)
            source_rows = history if request.start_ms < decision_ms else future
            rows = tuple(
                bar
                for bar in source_rows
                if request.start_ms <= int(bar.start_time.timestamp() * 1000) <= request.end_ms
            )
            return BybitKlineAcquisition(rows, {"BTCUSDT": 1})

    store = Store()
    klines = Klines()
    summary = run_shadow_outcome_cycle(
        store,
        observed_at=_NOW + timedelta(minutes=20),
        bybit_site="eu",
        kline_client=klines,
        source_limit=20,
        active_limit=30,
    )

    assert summary.host == "api.bybit.eu"
    assert summary.seeds_created == 1
    assert summary.outcomes_persisted == 1
    assert summary.final_outcomes_persisted == 0
    assert summary.trade_actionable is False
    assert len(store.outcomes) == 1
    assert store.outcomes[0].completed_bar_count == 3
    assert store.outcomes[0].horizons[0].complete is True
    assert all(request.symbols == ("BTCUSDT",) for request in klines.requests)
