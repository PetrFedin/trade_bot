from app.strategy.crypto_trade_quality_diagnostics import diagnose_crypto_replay_quality


def _trade(
    *,
    symbol: str,
    side: str,
    entry_time: str,
    net: float,
    fees: float,
    risk: float,
    mfe_r: float,
    mae_r: float,
    exit_reason: str,
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "side": side,
        "entry_time": entry_time,
        "net_pnl_usdt": net,
        "fees_usdt": fees,
        "risk_budget_usdt": risk,
        "maximum_favorable_r_before_exit": mfe_r,
        "maximum_adverse_r_before_exit": mae_r,
        "exit_reason": exit_reason,
    }


def test_quality_diagnostic_tracks_mfe_giveback_and_edge_realization() -> None:
    report = {
        "decision_events": [
            {
                "event": "ENTRY",
                "symbol": "BTCUSDT",
                "execution_time": "2026-08-17T10:05:00+00:00",
                "exit_mode": "FIXED_20_TARGET",
                "expected_net_edge_usd": 30.0,
            },
            {
                "event": "ENTRY",
                "symbol": "ETHUSDT",
                "execution_time": "2026-08-17T11:05:00+00:00",
                "exit_mode": "OPEN_ENDED_RUNNER",
                "expected_net_edge_usd": 40.0,
            },
        ],
        "closed_trades": [
            _trade(
                symbol="BTCUSDT",
                side="LONG",
                entry_time="2026-08-17T10:05:00+00:00",
                net=20.0,
                fees=2.0,
                risk=10.0,
                mfe_r=2.5,
                mae_r=0.5,
                exit_reason="NET_TARGET",
            ),
            _trade(
                symbol="ETHUSDT",
                side="SHORT",
                entry_time="2026-08-17T11:05:00+00:00",
                net=-5.0,
                fees=2.5,
                risk=10.0,
                mfe_r=1.0,
                mae_r=1.4,
                exit_reason="HARD_STOP",
            ),
        ],
    }

    quality = diagnose_crypto_replay_quality(report)

    assert quality["trade_count"] == 2
    assert quality["overall"]["total_net_pnl_usdt"] == 15.0
    assert quality["overall"]["profit_factor"] == 4.0
    assert quality["overall"]["positive_mfe_lost_trade_count"] == 1
    assert quality["by_exit_mode"]["FIXED_20_TARGET"]["trade_count"] == 1
    assert quality["by_exit_mode"]["OPEN_ENDED_RUNNER"]["trade_count"] == 1
    assert quality["by_exit_reason"]["NET_TARGET"]["average_mfe_capture_ratio"] == 0.8
    assert quality["by_exit_reason"]["HARD_STOP"]["average_mfe_capture_ratio"] == 0.0
    assert quality["strategy_selection_allowed"] is False
    assert quality["threshold_retuning_allowed"] is False
    assert quality["strategy_promotion_allowed"] is False
    assert quality["demo_activation_allowed"] is False
    assert quality["live_activation_allowed"] is False


def test_quality_diagnostic_tolerates_closed_trade_without_entry_event() -> None:
    report = {
        "decision_events": [],
        "closed_trades": [
            _trade(
                symbol="BTCUSDT",
                side="LONG",
                entry_time="2026-08-17T10:05:00+00:00",
                net=1.0,
                fees=1.0,
                risk=10.0,
                mfe_r=0.5,
                mae_r=0.2,
                exit_reason="END_OF_REPLAY",
            )
        ],
    }

    quality = diagnose_crypto_replay_quality(report)
    assert quality["by_exit_mode"]["UNKNOWN"]["trade_count"] == 1
    assert quality["overall"]["average_edge_realization_ratio"] is None
