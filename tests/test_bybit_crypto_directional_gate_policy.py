import json
from datetime import date
from pathlib import Path

_POLICY_PATH = Path("research/bybit_crypto_directional_gate_v1_policy.json")


def _policy() -> dict[str, object]:
    return json.loads(_POLICY_PATH.read_text(encoding="utf-8"))


def test_directional_gate_is_prospective_and_cannot_use_discovery_window() -> None:
    policy = _policy()
    discovery_end = date.fromisoformat(str(policy["discovery_window_end_utc"]))
    validation_start = date.fromisoformat(
        str(policy["prospective_validation_start_utc"])
    )

    assert policy["status"] == "PREDECLARED_PROSPECTIVE_SHADOW_ONLY"
    assert validation_start > discovery_end
    assert policy["discovery_evidence_may_validate_candidate"] is False
    assert policy["same_sample_directional_activation_allowed"] is False


def test_directional_gate_changes_only_directional_entry_eligibility() -> None:
    policy = _policy()
    hypothesis = policy["hypothesis"]
    anti_overfit = policy["anti_overfit"]

    assert isinstance(hypothesis, dict)
    assert isinstance(anti_overfit, dict)
    assert hypothesis["candidate_action"] == "BLOCK_NEW_SHORT_ENTRIES_KEEP_LONG_LOGIC_UNCHANGED"
    assert hypothesis["long_entry_logic_changes_allowed"] is False
    assert hypothesis["exit_logic_changes_allowed_in_same_experiment"] is False
    assert hypothesis["risk_threshold_changes_allowed_in_same_experiment"] is False
    assert anti_overfit["only_directional_entry_filter_may_differ_from_source_candidate"] is True
    assert anti_overfit["parameter_tuning_between_folds"] is False


def test_directional_gate_requires_future_short_weakness_and_full_candidate_quality() -> None:
    evidence = _policy()["prospective_evidence"]

    assert isinstance(evidence, dict)
    assert evidence["minimum_completed_utc_archive_days"] == 28
    assert evidence["fold_days"] == 7
    assert evidence["minimum_folds"] == 4
    assert evidence["minimum_active_short_folds"] == 3
    assert evidence["maximum_positive_short_fold_fraction"] == 0.25
    assert evidence["maximum_short_aggregate_profit_factor"] == 1.0
    assert evidence["require_negative_short_total_net_pnl"] is True
    assert evidence["minimum_candidate_closed_trades"] == 30
    assert evidence["minimum_candidate_positive_fold_fraction"] == 0.75
    assert evidence["minimum_candidate_profit_factor"] == 1.2
    assert evidence["maximum_candidate_worst_fold_drawdown_pct"] == 5.0
    assert evidence["require_zero_candidate_risk_budget_breaches"] is True
    assert evidence["require_positive_candidate_total_net_pnl"] is True
    assert evidence["require_candidate_net_pnl_not_worse_than_combined_baseline"] is True
    assert evidence["require_candidate_profit_factor_not_worse_than_combined_baseline"] is True
    assert evidence["require_candidate_drawdown_not_worse_than_combined_baseline"] is True


def test_directional_gate_cannot_auto_activate_demo_or_live() -> None:
    promotion = _policy()["promotion"]

    assert isinstance(promotion, dict)
    assert promotion["automatic_strategy_selection_allowed"] is False
    assert promotion["directional_filter_activation_allowed"] is False
    assert promotion["demo_observation_automatic_activation_allowed"] is False
    assert promotion["live_promotion_allowed"] is False
    assert promotion["bybit_live_order_routing_allowed"] is False
