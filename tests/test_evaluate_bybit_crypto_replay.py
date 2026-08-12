from tools.evaluate_bybit_crypto_replay import evaluate_report


def _variant(*, accepted: int, trades: int, pnl: float) -> dict[str, object]:
    return {
        "target_net_profit_usd": 20.0,
        "accepted_trade_plan_event_count": accepted,
        "metrics": {
            "closed_trade_count": trades,
            "total_net_pnl_usdt": pnl,
            "profit_factor": 1.5 if trades else None,
            "maximum_drawdown_pct": 1.0,
            "fees_usdt": 5.0,
            "risk_budget_breach_count": 0,
        },
    }


def _runner(*, accepted: int, trades: int, pnl: float) -> dict[str, object]:
    return {
        "mode": "MIN_20_NET_EDGE_OPEN_ENDED_RUNNER",
        "minimum_entry_net_profit_usd": 20.0,
        "runner_activation_net_profit_usd": 20.0,
        "runner_initial_protected_net_profit_usd": 15.0,
        "profit_cap_net_profit_usd": None,
        "fixed_take_profit_enabled": False,
        "accepted_trade_plan_event_count": accepted,
        "metrics": {
            "closed_trade_count": trades,
            "total_net_pnl_usdt": pnl,
            "profit_factor": 1.6 if trades else None,
            "maximum_drawdown_pct": 2.0,
            "fees_usdt": 6.0,
            "risk_budget_breach_count": 0,
        },
    }


def test_shadow_candidates_are_scored_but_never_live_promoted() -> None:
    report = {
        "qualification": "PASS_CRYPTO_HISTORICAL_REPLAY",
        "source": "BYBIT_OFFICIAL_PUBLIC_TRADE_ARCHIVE_AGGREGATED_5M",
        "first_completed_bar": "2026-07-20T00:00:00+00:00",
        "last_completed_bar": "2026-08-12T00:00:00+00:00",
        "opening_equity_usdt": 1000.0,
        "variants": {"TARGET_20_USD": _variant(accepted=0, trades=0, pnl=0.0)},
        "notional_cap_shadow_candidates": {
            "MAX_NOTIONAL_3X_EQUITY": {
                "maximum_notional_to_equity": 3.0,
                "risk_fraction_per_trade": 0.01,
                "purpose": "shadow",
                "variants": {"TARGET_20_USD": _variant(accepted=35, trades=35, pnl=100.0)},
            }
        },
        "strategy_shadow_candidates": {
            "MIN_20_NET_EDGE_OPEN_ENDED_RUNNER": _runner(
                accepted=40,
                trades=40,
                pnl=120.0,
            )
        },
    }

    scored = evaluate_report(report)

    baseline = scored["variants"]["TARGET_20_USD"]
    notional_candidate = scored["shadow_candidates"]["MAX_NOTIONAL_3X_EQUITY"]
    runner = scored["strategy_shadow_candidates"]["MIN_20_NET_EDGE_OPEN_ENDED_RUNNER"]
    assert baseline["posture"] == "RETUNE_TARGET_FEASIBILITY"
    assert notional_candidate["strategy_promotion_allowed"] is False
    assert notional_candidate["demo_order_writes_allowed"] is False
    assert notional_candidate["live_promotion_allowed"] is False
    assert notional_candidate["variants"]["TARGET_20_USD"]["posture"] == (
        "ELIGIBLE_FOR_DEMO_OBSERVATION"
    )
    assert notional_candidate["variants"]["TARGET_20_USD"]["live_promotion_allowed"] is False
    assert runner["posture"] == "ELIGIBLE_FOR_DEMO_OBSERVATION"
    assert runner["fixed_take_profit_enabled"] is False
    assert runner["profit_cap_net_profit_usd"] is None
    assert runner["runner_activation_net_profit_usd"] == 20.0
    assert runner["runner_initial_protected_net_profit_usd"] == 15.0
    assert runner["strategy_promotion_allowed"] is False
    assert runner["demo_order_writes_allowed"] is False
    assert runner["live_promotion_allowed"] is False
