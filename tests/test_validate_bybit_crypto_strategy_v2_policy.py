import json
from copy import deepcopy
from pathlib import Path

import pytest

from tools.validate_bybit_crypto_strategy_v2_policy import (
    load_and_validate_strategy_v2_policy,
    validate_strategy_v2_policy,
)

_POLICY_PATH = Path("research/bybit_crypto_strategy_v2_policy.json")


def _policy() -> dict[str, object]:
    raw = json.loads(_POLICY_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def test_committed_strategy_v2_policy_matches_code_defaults() -> None:
    result = load_and_validate_strategy_v2_policy(_POLICY_PATH)

    assert result["qualification"] == "PASS_BYBIT_CRYPTO_STRATEGY_V2_POLICY"
    assert result["policy_matches_code_defaults"] is True
    assert result["strategy_promotion_allowed"] is False
    assert result["live_promotion_allowed"] is False
    assert result["bybit_live_order_routing_allowed"] is False


def test_policy_drift_in_runner_gate_fails_closed() -> None:
    policy = deepcopy(_policy())
    runner = policy["runner"]
    assert isinstance(runner, dict)
    runner["minimum_expected_edge_multiple"] = 1.4

    with pytest.raises(ValueError, match="minimum_expected_edge_multiple"):
        validate_strategy_v2_policy(policy)


def test_policy_cannot_relax_funding_reconciliation_before_symbol_reuse() -> None:
    policy = deepcopy(_policy())
    accounting = policy["post_trade_accounting"]
    assert isinstance(accounting, dict)
    accounting["funding_reconciliation_required_before_symbol_reuse"] = False

    with pytest.raises(ValueError, match="funding reuse gate"):
        validate_strategy_v2_policy(policy)


def test_policy_cannot_label_intermediate_pnl_as_final_profit_outcome() -> None:
    policy = deepcopy(_policy())
    accounting = policy["post_trade_accounting"]
    assert isinstance(accounting, dict)
    accounting["profit_outcome_requires_fully_reconciled_all_in_pnl"] = False

    with pytest.raises(ValueError, match="profit outcome"):
        validate_strategy_v2_policy(policy)


def test_policy_cannot_enable_live_or_automatic_promotion() -> None:
    policy = deepcopy(_policy())
    promotion = policy["promotion"]
    assert isinstance(promotion, dict)
    promotion["live_promotion_allowed"] = True

    with pytest.raises(ValueError, match="live_promotion_allowed"):
        validate_strategy_v2_policy(policy)
