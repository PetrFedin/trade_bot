from app.strategy.crypto_replay_quality import normalize_crypto_replay_report


def _report() -> dict[str, object]:
    return {
        "last_completed_bar": "2026-08-11T23:55:00+00:00",
        "strategy": {"maximum_holding_bars": 36},
        "variants": {
            "TARGET_15_USD": {
                "metrics": {
                    "win_count": 1,
                    "loss_count": 2,
                    "win_rate": 1 / 3,
                    "profit_factor": 1e25,
                    "max_hold_exit_count": 1,
                    "realized_target_or_better_count": 1,
                },
                "closed_trades": [
                    {
                        "net_pnl_usdt": -5.8e-25,
                        "target_net_profit_usd": 15,
                        "exit_reason": "PROFIT_PROTECTION",
                        "exit_time": "2026-08-10T03:25:00+00:00",
                        "holding_bars": 1,
                    },
                    {
                        "net_pnl_usdt": 15.0000000000001,
                        "target_net_profit_usd": 15,
                        "exit_reason": "NET_TARGET",
                        "exit_time": "2026-08-11T14:50:00+00:00",
                        "holding_bars": 14,
                    },
                    {
                        "net_pnl_usdt": -2,
                        "target_net_profit_usd": 15,
                        "exit_reason": "MAX_HOLD",
                        "exit_time": "2026-08-11T23:55:00+00:00",
                        "holding_bars": 5,
                    },
                ],
            }
        },
        "strategy_shadow_candidates": {
            "MIN_20_NET_EDGE_OPEN_ENDED_RUNNER": {
                "strategy": {"maximum_holding_bars": 36},
                "metrics": {
                    "win_count": 0,
                    "loss_count": 1,
                    "win_rate": 0,
                    "profit_factor": None,
                    "max_hold_exit_count": 1,
                    "realized_target_or_better_count": 0,
                },
                "closed_trades": [
                    {
                        "net_pnl_usdt": -4e-25,
                        "target_net_profit_usd": 20,
                        "exit_reason": "PROFIT_PROTECTION",
                        "exit_time": "2026-08-10T03:25:00+00:00",
                        "holding_bars": 2,
                    },
                    {
                        "net_pnl_usdt": 25,
                        "target_net_profit_usd": 20,
                        "exit_reason": "MAX_HOLD",
                        "exit_time": "2026-08-11T23:55:00+00:00",
                        "holding_bars": 7,
                    },
                ],
            }
        },
    }


def test_micro_decimal_residual_is_breakeven_not_loss() -> None:
    normalized = normalize_crypto_replay_report(_report())
    variant = normalized["variants"]["TARGET_15_USD"]
    metrics = variant["metrics"]
    first = variant["closed_trades"][0]

    assert metrics["win_count"] == 1
    assert metrics["loss_count"] == 1
    assert metrics["breakeven_count"] == 1
    assert metrics["win_rate"] == 1 / 3
    assert metrics["decisive_win_rate"] == 0.5
    assert metrics["profit_factor"] == 7.50000000000005
    assert first["pnl_class"] == "BREAKEVEN"
    assert first["normalized_net_pnl_usdt"] == 0.0


def test_terminal_forced_close_is_not_misreported_as_max_hold() -> None:
    normalized = normalize_crypto_replay_report(_report())
    variant = normalized["variants"]["TARGET_15_USD"]
    trade = variant["closed_trades"][2]
    metrics = variant["metrics"]

    assert trade["exit_reason"] == "MAX_HOLD"
    assert trade["normalized_exit_reason"] == "END_OF_REPLAY"
    assert metrics["end_of_replay_exit_count"] == 1
    assert metrics["max_hold_exit_count"] == 0


def test_normalization_preserves_raw_trade_pnl_and_marks_target_hit() -> None:
    normalized = normalize_crypto_replay_report(_report())
    variant = normalized["variants"]["TARGET_15_USD"]
    second = variant["closed_trades"][1]

    assert second["net_pnl_usdt"] == 15.0000000000001
    assert second["pnl_class"] == "WIN"
    assert variant["metrics"]["realized_target_or_better_count"] == 1
    assert normalized["replay_quality_normalization"]["raw_trade_pnl_preserved"] is True


def test_open_ended_runner_shadow_candidate_is_normalized_too() -> None:
    normalized = normalize_crypto_replay_report(_report())
    runner = normalized["strategy_shadow_candidates"]["MIN_20_NET_EDGE_OPEN_ENDED_RUNNER"]

    assert runner["closed_trades"][0]["pnl_class"] == "BREAKEVEN"
    assert runner["closed_trades"][1]["normalized_exit_reason"] == "END_OF_REPLAY"
    assert runner["metrics"]["win_count"] == 1
    assert runner["metrics"]["loss_count"] == 0
    assert runner["metrics"]["breakeven_count"] == 1
    assert runner["metrics"]["realized_target_or_better_count"] == 1
    assert normalized["replay_quality_normalization"][
        "strategy_shadow_candidates_normalized"
    ] is True
