from __future__ import annotations

import hashlib
from decimal import Decimal

from app.qualification.portable_artifact_codec import canonical_json_bytes
from app.strategy.cross_sectional_portfolio import (
    CrossSectionalPortfolioBacktester,
    CrossSectionalPortfolioPolicy,
)
from app.strategy.cross_sectional_selection import CrossSectionalSelector
from app.strategy.historical_strategy_evidence_v1 import (
    HistoricalBenchmarkV1,
    HistoricalCostCoverageV1,
    StrategyProfitabilityVerdict,
    build_historical_strategy_evidence,
)
from app.strategy.position_management import PositionManagementPolicy
from app.strategy.reentry_confirmation import ReentryConfirmationPolicy
from tests.test_cross_sectional_portfolio import stable_universe


def _result():
    return CrossSectionalPortfolioBacktester(
        selector=CrossSectionalSelector(top_k=2),
        portfolio_policy=CrossSectionalPortfolioPolicy(
            opening_cash=Decimal("10000"),
            fee_per_fill=Decimal("0"),
            fee_bps_per_fill=Decimal("8"),
            slippage_bps=Decimal("5"),
            maximum_gross_exposure_fraction=Decimal("0.60"),
            new_position_target_equity_fraction=Decimal("0.29"),
        ),
        position_policy=PositionManagementPolicy(),
        reentry_policy=ReentryConfirmationPolicy(
            minimum_consecutive_eligible_bars=2
        ),
    ).run(stable_universe(aapl_stop_on_entry=True))


def test_historical_evidence_reconciles_trade_cost_components() -> None:
    result = _result()
    strategy_config = {"top_k": 2, "fee_bps_per_fill": "8"}

    evidence = build_historical_strategy_evidence(
        result=result,
        strategy_id="cross-sectional-shadow-v1",
        strategy_config=strategy_config,
        dataset_id="synthetic-cost-attribution",
        dataset_sha256="1" * 64,
        start="2026-01-02T00:00:00+00:00",
        end="2026-01-12T00:00:00+00:00",
        symbols=("AAPL", "MSFT", "NVDA"),
        cost_coverage=HistoricalCostCoverageV1(
            fixed_fees_modelled=True,
            proportional_fees_modelled=True,
            slippage_modelled=True,
            funding_modelled=False,
            queue_position_modelled=False,
            partial_fills_modelled=False,
        ),
        benchmarks=(
            HistoricalBenchmarkV1(
                benchmark_id="cash",
                total_return=Decimal("0"),
            ),
        ),
    )

    assert evidence.opening_cash == result.opening_cash
    assert evidence.ending_equity == result.ending_equity
    assert evidence.total_pnl == result.total_pnl
    assert evidence.closed_trade_entry_fees == sum(
        (trade.entry_fee for trade in result.closed_trades),
        Decimal("0"),
    )
    assert evidence.closed_trade_exit_fees == sum(
        (trade.exit_fee for trade in result.closed_trades),
        Decimal("0"),
    )
    assert evidence.closed_trade_slippage_cost == sum(
        (trade.slippage_cost for trade in result.closed_trades),
        Decimal("0"),
    )
    assert evidence.gross_closed_trade_pnl_before_costs == sum(
        (trade.gross_pnl_before_costs for trade in result.closed_trades),
        Decimal("0"),
    )
    assert evidence.strategy_config_sha256 == hashlib.sha256(
        canonical_json_bytes(strategy_config)
    ).hexdigest()


def test_missing_funding_for_perpetual_style_cost_stack_cannot_prove_profitability() -> None:
    result = _result()

    evidence = build_historical_strategy_evidence(
        result=result,
        strategy_id="cross-sectional-shadow-v1",
        strategy_config={"top_k": 2},
        dataset_id="synthetic-no-funding",
        dataset_sha256="2" * 64,
        start="2026-01-02T00:00:00+00:00",
        end="2026-01-12T00:00:00+00:00",
        symbols=("AAPL", "MSFT", "NVDA"),
        cost_coverage=HistoricalCostCoverageV1(
            fixed_fees_modelled=True,
            proportional_fees_modelled=True,
            slippage_modelled=True,
            funding_modelled=False,
            queue_position_modelled=False,
            partial_fills_modelled=False,
        ),
        benchmarks=(),
        out_of_sample=True,
        walk_forward=True,
    )

    assert (
        evidence.verdict
        is StrategyProfitabilityVerdict.PROFITABILITY_NOT_PROVEN
    )
    assert evidence.cost_adjusted_return_lower_bound is None
    assert evidence.cost_adjusted_return_upper_bound is None


def test_positive_complete_cost_result_without_oos_is_only_research_candidate() -> None:
    result = _result()
    evidence = build_historical_strategy_evidence(
        result=result,
        strategy_id="cross-sectional-shadow-v1",
        strategy_config={"top_k": 2},
        dataset_id="synthetic-complete-costs",
        dataset_sha256="3" * 64,
        start="2026-01-02T00:00:00+00:00",
        end="2026-01-12T00:00:00+00:00",
        symbols=("AAPL", "MSFT", "NVDA"),
        cost_coverage=HistoricalCostCoverageV1(
            fixed_fees_modelled=True,
            proportional_fees_modelled=True,
            slippage_modelled=True,
            funding_modelled=True,
            queue_position_modelled=False,
            partial_fills_modelled=False,
        ),
        benchmarks=(
            HistoricalBenchmarkV1(
                benchmark_id="cash",
                total_return=Decimal("-1"),
            ),
        ),
        funding_cost_lower_bound=Decimal("0"),
        funding_cost_upper_bound=Decimal("0"),
        out_of_sample=False,
        walk_forward=False,
    )

    if evidence.cost_adjusted_return_lower_bound is not None and (
        evidence.cost_adjusted_return_lower_bound > 0
    ):
        assert (
            evidence.verdict
            is StrategyProfitabilityVerdict.RESEARCH_EDGE_CANDIDATE
        )
    else:
        assert (
            evidence.verdict
            is StrategyProfitabilityVerdict.PROFITABILITY_NOT_PROVEN
        )
