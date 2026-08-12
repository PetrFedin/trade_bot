import json
from pathlib import Path

POLICY = Path("research/selection_exit_confirmation_shadow_v1.json")


def test_selection_exit_confirmation_policy_remains_shadow_only() -> None:
    payload = json.loads(POLICY.read_text(encoding="utf-8"))

    assert payload["schema"] == "selection-exit-confirmation-shadow-v1"
    assert payload["shadow_only"] is True
    assert payload["strategy_promotion_allowed"] is False
    assert payload["scope"] == {
        "delayed_exit_reason": "SELECTION_EXIT_ONLY",
        "hard_stop_delayed": False,
        "profit_protection_delayed": False,
        "trailing_stop_delayed": False,
        "take_profit_delayed": False,
        "time_stop_delayed": False,
        "kill_switch_delayed": False,
    }
    assert payload["policy"]["minimum_consecutive_deselected_bars"] == 2
    assert payload["policy"]["exit_profitable_positions_immediately"] is True
    assert "NO_SELECTION_EXIT_SAME_SAMPLE_EVIDENCE" in payload[
        "promotion_blockers"
    ]
    assert "NO_SELECTION_EXIT_WALK_FORWARD_EVIDENCE" in payload[
        "promotion_blockers"
    ]
    assert "NO_REAL_PAPER_SELECTION_EXIT_EVIDENCE" in payload[
        "promotion_blockers"
    ]
