from tools.report_bybit_crypto_trade_quality import build_trade_quality_report


def _payload(exit_mode: str) -> dict[str, object]:
    entry_time = "2026-08-17T10:05:00+00:00"
    return {
        "decision_events": [
            {
                "event": "ENTRY",
                "symbol": "BTCUSDT",
                "execution_time": entry_time,
                "exit_mode": exit_mode,
                "expected_net_edge_usd": 30.0,
            }
        ],
        "closed_trades": [
            {
                "symbol": "BTCUSDT",
                "side": "LONG",
                "entry_time": entry_time,
                "net_pnl_usdt": 20.0,
                "fees_usdt": 2.0,
                "risk_budget_usdt": 10.0,
                "maximum_favorable_r_before_exit": 2.5,
                "maximum_adverse_r_before_exit": 0.4,
                "exit_reason": "NET_TARGET",
            }
        ],
    }


def test_report_scores_variants_and_shadow_candidates_without_selection_authority() -> None:
    report = {
        "source": "BYBIT_OFFICIAL_PUBLIC_TRADE_ARCHIVE_AGGREGATED_5M",
        "archive_dates": ["2026-08-03"],
        "symbols": ["BTCUSDT"],
        "variants": {"TARGET_20_USD": _payload("FIXED_20_TARGET")},
        "strategy_shadow_candidates": {
            "CONDITIONAL": _payload("OPEN_ENDED_RUNNER")
        },
    }

    quality = build_trade_quality_report(report)

    assert quality["qualification"] == "BYBIT_CRYPTO_TRADE_QUALITY_REPORTED"
    assert quality["variants"]["TARGET_20_USD"]["overall"]["total_net_pnl_usdt"] == 20.0
    assert quality["strategy_shadow_candidates"]["CONDITIONAL"]["by_exit_mode"][
        "OPEN_ENDED_RUNNER"
    ]["trade_count"] == 1
    assert quality["diagnostic_only"] is True
    assert quality["strategy_selection_allowed"] is False
    assert quality["threshold_retuning_allowed"] is False
    assert quality["strategy_promotion_allowed"] is False
    assert quality["demo_activation_allowed"] is False
    assert quality["live_activation_allowed"] is False
