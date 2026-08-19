from tools.audit_bybit_crypto_runner_opportunity import (
    audit_report_runner_opportunity,
    audit_runner_opportunity,
)


def _entry(
    *,
    side: str,
    expected: float,
    required: float,
    exit_mode: str,
    reasons: list[str] | None = None,
) -> dict[str, object]:
    return {
        "event": "ENTRY",
        "symbol": "BTCUSDT",
        "side": side,
        "expected_net_edge_usd": expected,
        "runner_required_expected_net_edge_usd": required,
        "runner_admission_reasons": [] if reasons is None else reasons,
        "exit_mode": exit_mode,
    }


def test_runner_opportunity_quantifies_gate_distance_without_retuning() -> None:
    candidate = {
        "decision_events": [
            _entry(
                side="LONG",
                expected=20,
                required=30,
                exit_mode="FIXED_20_TARGET",
                reasons=["RUNNER_EXCESS_EXPECTED_EDGE_TOO_THIN"],
            ),
            _entry(
                side="LONG",
                expected=27.5,
                required=30,
                exit_mode="FIXED_20_TARGET",
                reasons=["RUNNER_EXCESS_EXPECTED_EDGE_TOO_THIN"],
            ),
            _entry(
                side="SHORT",
                expected=29,
                required=30,
                exit_mode="FIXED_20_TARGET",
                reasons=["RUNNER_EXCESS_EXPECTED_EDGE_TOO_THIN"],
            ),
            _entry(
                side="LONG",
                expected=31,
                required=30,
                exit_mode="RUNNER",
            ),
            {"event": "EXIT", "side": "LONG"},
        ]
    }

    result = audit_runner_opportunity(candidate)

    assert result["entry_count"] == 4
    assert result["runner_selected_entry_count"] == 1
    assert result["fixed_target_selected_entry_count"] == 3
    assert result["runner_selected_fraction"] == 0.25
    assert result["expected_edge_to_required_edge_ratio_bucket_counts"] == {
        "far_below_gate": 1,
        "below_gate": 0,
        "near_miss": 2,
        "gate_cleared": 1,
    }
    assert result["fixed_target_near_miss_count"] == 2
    assert result["runner_admission_reason_counts"] == {
        "RUNNER_EXCESS_EXPECTED_EDGE_TOO_THIN": 3
    }
    assert result["by_side"]["LONG"]["entry_count"] == 3
    assert result["by_side"]["SHORT"]["entry_count"] == 1
    assert result["fixed_target_post_exit_path_is_censored"] is True
    assert result["fixed_target_mfe_may_validate_runner_counterfactual"] is False
    assert result["runner_threshold_retuning_allowed"] is False
    assert result["automatic_runner_activation_allowed"] is False
    assert result["live_promotion_allowed"] is False


def test_report_audit_skips_unconditional_runner_but_keeps_conditional_candidates() -> None:
    report = {
        "source": "BYBIT_OFFICIAL_PUBLIC_TRADE_ARCHIVE_AGGREGATED_5M",
        "archive_dates": ["2026-08-10", "2026-08-11"],
        "strategy_shadow_candidates": {
            "UNCONDITIONAL": {
                "runner_minimum_expected_edge_multiple": None,
                "decision_events": [],
            },
            "CONDITIONAL": {
                "runner_minimum_expected_edge_multiple": 1.5,
                "decision_events": [
                    _entry(
                        side="LONG",
                        expected=29,
                        required=30,
                        exit_mode="FIXED_20_TARGET",
                        reasons=["RUNNER_EXCESS_EXPECTED_EDGE_TOO_THIN"],
                    )
                ],
            },
        },
    }

    result = audit_report_runner_opportunity(report)

    assert set(result["candidates"]) == {"CONDITIONAL"}
    conditional = result["candidates"]["CONDITIONAL"]
    assert conditional["fixed_target_near_miss_count"] == 1
    assert result["runner_threshold_retuning_allowed"] is False
    assert result["automatic_runner_activation_allowed"] is False


def test_empty_conditional_candidate_is_a_valid_zero_opportunity_diagnostic() -> None:
    result = audit_runner_opportunity({"decision_events": []})

    assert result["entry_count"] == 0
    assert result["runner_selected_fraction"] is None
    assert result["expected_edge_to_required_edge_ratio"]["count"] == 0
    assert result["by_side"]["LONG"]["entry_count"] == 0
    assert result["by_side"]["SHORT"]["entry_count"] == 0
