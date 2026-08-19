import json
from pathlib import Path

POLICY = Path("research/profit_runner_shadow_v1.json")


def test_profit_runner_policy_remains_shadow_only() -> None:
    payload = json.loads(POLICY.read_text(encoding="utf-8"))

    assert payload["schema"] == "profit-runner-shadow-v1"
    assert payload["shadow_only"] is True
    assert payload["strategy_promotion_allowed"] is False
    assert payload["policy"]["take_profit_mode"] == "PROFIT_RUNNER"
    assert payload["scope"]["hard_stop_changed"] is False
    assert payload["scope"]["time_stop_changed"] is False
    assert payload["scope"]["same_bar_peak_can_arm_protection"] is False
    assert payload["scope"]["paper_or_live_activation_allowed"] is False
    assert "NO_PROFIT_RUNNER_SAME_SAMPLE_EVIDENCE" in payload[
        "promotion_blockers"
    ]
    assert "NO_PROFIT_RUNNER_WALK_FORWARD_EVIDENCE" in payload[
        "promotion_blockers"
    ]
    assert "NO_REAL_PAPER_PROFIT_RUNNER_EVIDENCE" in payload[
        "promotion_blockers"
    ]
