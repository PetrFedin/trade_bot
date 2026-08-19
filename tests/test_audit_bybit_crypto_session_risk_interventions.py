from tools.audit_bybit_crypto_session_risk_interventions import (
    audit_report_session_risk_interventions,
    audit_session_risk_interventions,
)


def _metrics(*, pnl: float, drawdown: float, fees: float, trades: int = 4) -> dict[str, object]:
    return {
        "closed_trade_count": trades,
        "total_net_pnl_usdt": pnl,
        "profit_factor": 1.5,
        "maximum_drawdown_pct": drawdown,
        "fees_usdt": fees,
        "risk_budget_breach_count": 0,
    }


def _session_candidate() -> dict[str, object]:
    return {
        "session_risk": {
            "enabled": True,
            "risk_event_count": 1,
            "entry_block_count": 2,
            "flatten_trade_count": 1,
            "reason_counts": {
                "SESSION_DRAWDOWN_LIMIT_BREACHED": 1,
                "SESSION_CONSECUTIVE_LOSS_LIMIT_REACHED": 1,
            },
        },
        "metrics": _metrics(pnl=12.0, drawdown=3.0, fees=5.0, trades=3),
        "decision_events": [
            {
                "event": "SESSION_RISK_LATCHED",
                "decision_time": "2026-08-10T10:00:00+00:00",
                "action": "FLATTEN_AND_BLOCK",
                "reasons": [
                    "SESSION_DRAWDOWN_LIMIT_BREACHED",
                    "SESSION_CONSECUTIVE_LOSS_LIMIT_REACHED",
                ],
                "flatten_at_next_open": True,
                "current_equity_usdt": 950,
                "peak_equity_usdt": 1000,
                "realized_pnl_usdt": -20,
                "execution_cost_usdt": 8,
                "consecutive_losses": 3,
            },
            {
                "event": "SESSION_RISK_ENTRY_BLOCK",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "decision_time": "2026-08-10T10:05:00+00:00",
                "latched_reasons": [
                    "SESSION_DRAWDOWN_LIMIT_BREACHED",
                    "SESSION_CONSECUTIVE_LOSS_LIMIT_REACHED",
                ],
            },
            {
                "event": "SESSION_RISK_ENTRY_BLOCK",
                "symbol": "ETHUSDT",
                "side": "SHORT",
                "decision_time": "2026-08-10T10:10:00+00:00",
                "latched_reasons": [
                    "SESSION_DRAWDOWN_LIMIT_BREACHED",
                    "SESSION_CONSECUTIVE_LOSS_LIMIT_REACHED",
                ],
            },
            {
                "event": "EXIT",
                "symbol": "SOLUSDT",
                "side": "LONG",
                "execution_time": "2026-08-10T10:05:00+00:00",
                "exit_reason": "SESSION_RISK_FLATTEN",
                "net_pnl_usdt": -4.0,
                "gap_through": False,
                "ambiguous_intrabar_path": False,
            },
        ],
    }


def test_session_risk_audit_reconciles_latch_blocks_and_flatten() -> None:
    result = audit_session_risk_interventions(_session_candidate())

    assert result["latch_event_count"] == 1
    assert result["flatten_requested_latch_count"] == 1
    assert result["entry_block_event_count"] == 2
    assert result["flatten_exit_event_count"] == 1
    assert result["latch_reason_counts"] == {
        "SESSION_CONSECUTIVE_LOSS_LIMIT_REACHED": 1,
        "SESSION_DRAWDOWN_LIMIT_BREACHED": 1,
    }
    assert result["entry_block_side_counts"] == {"LONG": 1, "SHORT": 1}
    assert result["entry_block_symbol_counts"] == {"BTCUSDT": 1, "ETHUSDT": 1}
    assert result["flatten_side_counts"] == {"LONG": 1}
    assert result["flatten_total_net_pnl_usdt"] == -4.0
    assert result["flatten_positive_count"] == 0
    assert result["flatten_non_positive_count"] == 1
    assert result["latch_drawdown_pct"] == {
        "count": 1,
        "minimum": 5.0,
        "maximum": 5.0,
        "mean": 5.0,
    }
    assert result["latch_consecutive_losses"]["maximum"] == 3
    assert result["session_threshold_retuning_allowed"] is False
    assert result["automatic_strategy_activation_allowed"] is False


def test_report_audit_compares_session_candidates_with_frozen_baseline() -> None:
    baseline = {
        "session_risk": {"enabled": False},
        "metrics": _metrics(pnl=2.0, drawdown=5.0, fees=7.0, trades=4),
        "decision_events": [],
    }
    report = {
        "source": "BYBIT_OFFICIAL_PUBLIC_TRADE_ARCHIVE_AGGREGATED_5M",
        "archive_dates": ["2026-08-10", "2026-08-11"],
        "strategy_shadow_candidates": {
            "MIN_20_NET_EDGE_CONDITIONAL_RUNNER_1_5X": baseline,
            "MIN_20_NET_EDGE_CONDITIONAL_RUNNER_SESSION_RISK": _session_candidate(),
        },
    }

    result = audit_report_session_risk_interventions(report)

    candidate = result["candidates"][
        "MIN_20_NET_EDGE_CONDITIONAL_RUNNER_SESSION_RISK"
    ]
    delta = candidate["vs_conditional_baseline"]
    assert delta["closed_trade_delta"] == -1
    assert delta["total_net_pnl_delta_usdt"] == 10.0
    assert delta["maximum_drawdown_delta_pct"] == -2.0
    assert delta["fees_delta_usdt"] == -2.0
    assert delta["causal_attribution_allowed"] is False
    assert result["attribution_is_observational_not_causal"] is True
    assert result["session_threshold_retuning_allowed"] is False
    assert result["live_promotion_allowed"] is False


def test_session_risk_audit_fails_closed_when_summary_and_events_diverge() -> None:
    candidate = _session_candidate()
    session = candidate["session_risk"]
    assert isinstance(session, dict)
    session["entry_block_count"] = 3

    try:
        audit_session_risk_interventions(candidate)
    except ValueError as exc:
        assert "entry_block_count" in str(exc)
    else:
        raise AssertionError("session-risk audit must reject inconsistent event counts")
