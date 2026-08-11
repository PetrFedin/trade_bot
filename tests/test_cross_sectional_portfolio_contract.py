from decimal import Decimal
from pathlib import Path

from tools.qualify_cross_sectional_portfolio_shadow import qualify

POLICY = Path("research/cross_sectional_portfolio_shadow_v1.json")


def test_portfolio_contract_is_shadow_only_and_non_promotable() -> None:
    evidence = qualify(POLICY)
    assert evidence["qualification"] == "PASS_SYNTHETIC_PORTFOLIO_CONTRACT"
    assert evidence["shadow_only"] is True
    assert evidence["strategy_promotion_allowed"] is False
    assert evidence["real_multisymbol_ohlcv_evidence"] is False
    assert evidence["walk_forward_portfolio_benchmark_evidence"] is False
    assert evidence["external_paper_portfolio_evidence"] is False
    assert set(evidence["promotion_blockers"]) == {
        "SYNTHETIC_PORTFOLIO_CONTRACT_ONLY",
        "NO_REAL_MULTISYMBOL_OHLCV_EVIDENCE",
        "NO_WALK_FORWARD_PORTFOLIO_BENCHMARK",
        "NO_EXTERNAL_PAPER_PORTFOLIO_EVIDENCE",
    }


def test_top_k_scenario_stays_inside_predeclared_position_and_exposure_limits() -> None:
    evidence = qualify(POLICY)
    scenario = evidence["scenarios"]["TOP_K_BOUNDED_EXPOSURE"]
    first = scenario["decision_trace"][0]
    assert first["selected_symbols"] == ["AAPL", "MSFT"]
    assert first["entered_symbols"] == ["AAPL", "MSFT"]
    assert scenario["maximum_concurrent_positions"] == 2
    assert Decimal(scenario["maximum_gross_exposure_fraction_observed"]) <= Decimal(
        "0.60"
    )
    assert Decimal(scenario["turnover_fraction"]) > Decimal("0.50")


def test_portfolio_evidence_retains_profit_preservation_quality_metrics() -> None:
    evidence = qualify(POLICY)
    scenario = evidence["scenarios"]["TOP_K_BOUNDED_EXPOSURE"]
    for field in (
        "win_rate",
        "average_maximum_favorable_excursion_fraction",
        "average_maximum_adverse_excursion_fraction",
        "average_mfe_capture_ratio",
        "positive_mfe_trades",
        "profit_preservation_rate",
    ):
        assert field in scenario
    assert Decimal(scenario["win_rate"]) >= Decimal("0")
    assert Decimal(scenario["average_maximum_favorable_excursion_fraction"]) >= 0
    assert Decimal(scenario["average_maximum_adverse_excursion_fraction"]) >= 0

    for trade in scenario["closed_trades"]:
        assert "maximum_favorable_excursion_fraction" in trade
        assert "maximum_adverse_excursion_fraction" in trade
        assert "mfe_capture_ratio" in trade
        assert "mfe_giveback_fraction" in trade


def test_symbol_specific_stop_requires_confirmed_reentry() -> None:
    evidence = qualify(POLICY)
    scenario = evidence["scenarios"][
        "SYMBOL_SPECIFIC_INTRABAR_STOP_REENTRY_DELAY"
    ]
    first, second, third = scenario["decision_trace"][:3]
    assert first["intrabar_exit_symbols"] == ["AAPL"]
    assert second["blocked_entries"] == [
        ["AAPL", "REENTRY_CONFIRMATION_PENDING"]
    ]
    assert "AAPL" in third["entered_symbols"]
    assert scenario["one_bar_reentry_count"] == 0
    assert scenario["intrabar_exit_counts"]["INTRABAR_HARD_STOP"] >= 1


def test_ranking_change_rotates_at_next_open() -> None:
    evidence = qualify(POLICY)
    scenario = evidence["scenarios"]["NEXT_OPEN_SELECTION_ROTATION"]
    first, second = scenario["decision_trace"][:2]
    assert first["selected_symbols"] == ["AAPL", "MSFT"]
    assert second["selected_symbols"] == ["MSFT", "NVDA"]
    assert second["open_exit_symbols"] == ["AAPL"]
    assert second["entered_symbols"] == ["NVDA"]
    assert any(
        trade["symbol"] == "AAPL" and trade["exit_reason"] == "SELECTION_EXIT"
        for trade in scenario["closed_trades"]
    )
