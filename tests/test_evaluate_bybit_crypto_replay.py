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


def test_notional_shadow_candidate_is_scored_but_never_promoted() -> None:
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
    }

    scored = evaluate_report(report)

    baseline = scored["variants"]["TARGET_20_USD"]
    candidate = scored["shadow_candidates"]["MAX_NOTIONAL_3X_EQUITY"]
    assert baseline["posture"] == "RETUNE_TARGET_FEASIBILITY"
    assert candidate["strategy_promotion_allowed"] is False
    assert candidate["demo_order_writes_allowed"] is False
    assert candidate["live_promotion_allowed"] is False
    assert candidate["variants"]["TARGET_20_USD"]["posture"] == (
        "ELIGIBLE_FOR_DEMO_OBSERVATION"
    )
    assert candidate["variants"]["TARGET_20_USD"]["live_promotion_allowed"] is False
