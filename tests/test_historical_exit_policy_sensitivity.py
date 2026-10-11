from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from app.strategy.position_management import PositionManagementPolicy
from tools.historical_exit_policy_sensitivity import _arms, run_fixed_exit_study


def evidence(policy: PositionManagementPolicy) -> dict:
    stop = policy.stop_loss_fraction
    scenarios = {
        Decimal("0.02"): ("0.01", "0.00", "a"),
        Decimal("0.03"): ("-0.03", "-0.04", "b"),
        Decimal("0.04"): ("0.02", "0.01", "c"),
    }
    pre, lower, ident = scenarios[stop]
    return {
        "dataset_sha256": "d" * 64,
        "strategy_config_sha256": ident * 64,
        "evidence_sha256": "e" * 64,
        "verdict": "PROFITABILITY_NOT_PROVEN",
        "total_return": pre,
        "cost_adjusted_return_lower_bound": lower,
        "cost_adjusted_return_upper_bound": str(Decimal(lower) + Decimal("0.01")),
        "benchmarks": [{"benchmark_id": "equal_weight_capital_matched", "total_return": "0.15"}],
        "closed_trade_count": 100,
        "max_drawdown_fraction": "0.12",
        "turnover_fraction": "10",
        "fees_paid": "12",
    }


def test_three_predeclared_exit_arms_keep_protection_on() -> None:
    arms = _arms()
    assert [name for name, _ in arms] == [
        "SHIPPED_STOP_2P", "WIDER_STOP_3P", "WIDER_STOP_4P",
    ]
    assert [policy.stop_loss_fraction for _, policy in arms] == [
        Decimal("0.02"), Decimal("0.03"), Decimal("0.04"),
    ]
    assert all(policy.take_profit_fraction == Decimal("0.04") for _, policy in arms)


def test_no_winner_selected_and_delta_anchored_to_shipped() -> None:
    with patch(
        "tools.historical_exit_policy_sensitivity.build_evidence",
        side_effect=lambda **kwargs: evidence(kwargs["position_policy"]),
    ) as builder:
        report = run_fixed_exit_study(
            bars_dir=Path("bars"), funding_dir=Path("funding"),
            symbols=("BTCUSDT", "ETHUSDT"),
        )
    assert builder.call_count == 3
    assert report["research_only"] is True
    assert report["strategy_promotion_allowed"] is False
    assert report["no_winner_selected"] is True
    assert report["evaluation_class"] == "REUSED_INSPECTED_HISTORY_EXPLORATORY"
    assert report["arms"]["SHIPPED_STOP_2P"]["delta_vs_shipped_pre_funding"] == "0.00"
    assert Decimal(report["arms"]["WIDER_STOP_3P"]["delta_vs_shipped_pre_funding"]) < 0
    assert Decimal(report["arms"]["WIDER_STOP_4P"]["delta_vs_shipped_pre_funding"]) > 0


def test_positive_trade_arm_cannot_generate_promotion_claim() -> None:
    with patch("tools.historical_exit_policy_sensitivity.build_evidence",
               side_effect=lambda **kwargs: {
                   **evidence(kwargs["position_policy"]),
                   "verdict": "OOS_EDGE_CONFIRMED",
               }):
        with pytest.raises(ValueError, match="promotion evidence"):
            run_fixed_exit_study(
                bars_dir=Path("bars"), funding_dir=Path("funding"),
                symbols=("BTCUSDT", "ETHUSDT"),
            )


def test_duplicate_symbol_rejected_without_io() -> None:
    with patch("tools.historical_exit_policy_sensitivity.build_evidence") as builder:
        with pytest.raises(ValueError, match="unique"):
            run_fixed_exit_study(
                bars_dir=Path("bars"), funding_dir=Path("funding"),
                symbols=("BTCUSDT", "BTCUSDT"),
            )
    builder.assert_not_called()
