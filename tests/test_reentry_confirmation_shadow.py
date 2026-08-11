from decimal import Decimal
from pathlib import Path

from tools.qualify_reentry_confirmation_shadow import qualify

MANIFEST = Path("data/historical/aapl_plotly_multiregime/manifest.json")
POLICY = Path(
    "data/historical/aapl_plotly_multiregime/qualification_reentry_confirmation_shadow.json"
)


def test_reentry_candidate_is_predeclared_shadow_only() -> None:
    evidence = qualify(MANIFEST, POLICY)
    assert evidence["schema_version"] == "reentry-confirmation-evidence-v1"
    assert evidence["shadow_only"] is True
    assert evidence["promotion_allowed"] is False
    policy = evidence["predeclared_policy"]
    assert policy["reentry_confirmation"] == {
        "minimum_consecutive_eligible_bars": 2,
        "initial_entry_requires_confirmation": False,
        "reset_streak_on_ineligible_signal": True,
        "apply_after_any_exit": True,
    }
    assert policy["signal"]["fast_bars"] == 3
    assert policy["signal"]["slow_bars"] == 8
    assert policy["position_management"]["stop_loss_fraction"] == "0.02"
    assert policy["position_management"]["take_profit_fraction"] == "0.04"


def test_reentry_candidate_removes_observed_one_bar_reentry() -> None:
    evidence = qualify(MANIFEST, POLICY)
    aggregate = evidence["aggregate"]
    assert aggregate["baseline_one_bar_reentry_count"] == 1
    assert aggregate["candidate_one_bar_reentry_count"] == 0
    assert aggregate["candidate_pending_reentry_blocks"] == 1
    assert aggregate["one_bar_reentry_reduced"] is True


def test_reentry_candidate_reduces_loss_severity_but_is_not_profitable_evidence() -> None:
    evidence = qualify(MANIFEST, POLICY)
    aggregate = evidence["aggregate"]
    baseline = aggregate["baseline"]
    candidate = aggregate["candidate"]

    assert baseline["closed_trade_count"] == candidate["closed_trade_count"] == 3
    assert baseline["winning_trades"] == candidate["winning_trades"] == 2
    assert baseline["losing_trades"] == candidate["losing_trades"] == 1
    assert Decimal(candidate["net_closed_trade_pnl"]) > Decimal(
        baseline["net_closed_trade_pnl"]
    )
    assert Decimal(candidate["profit_factor"]) > Decimal(baseline["profit_factor"])
    assert Decimal(candidate["profit_factor"]) < Decimal("1")
    assert Decimal(candidate["net_closed_trade_pnl"]) < Decimal("0")
    assert aggregate["candidate_closed_trade_pnl_not_worse"] is True
    assert aggregate["candidate_mean_return_not_worse"] is True
    assert aggregate["candidate_worst_drawdown_not_worse"] is True
    blockers = set(evidence["promotion_blockers"])
    assert "CANDIDATE_AGGREGATE_PROFIT_FACTOR_BELOW_ONE" in blockers
    assert "CANDIDATE_CLOSED_TRADE_PNL_NOT_POSITIVE" in blockers
