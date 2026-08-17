from tools.audit_bybit_crypto_walk_forward_session_risk import (
    audit_walk_forward_session_risk,
)


def _candidate(
    *,
    pnl: float,
    long_trades: int,
    long_pnl: float,
    short_trades: int,
    short_pnl: float,
    enabled: bool,
    risk_events: int = 0,
    entry_blocks: int = 0,
    flatten_trades: int = 0,
    reasons: dict[str, int] | None = None,
) -> dict[str, object]:
    return {
        "metrics": {
            "closed_trade_count": long_trades + short_trades,
            "total_net_pnl_usdt": pnl,
        },
        "side_metrics": {
            "LONG": {
                "closed_trade_count": long_trades,
                "total_net_pnl_usdt": long_pnl,
            },
            "SHORT": {
                "closed_trade_count": short_trades,
                "total_net_pnl_usdt": short_pnl,
            },
        },
        "session_risk": {
            "enabled": enabled,
            "risk_event_count": risk_events,
            "entry_block_count": entry_blocks,
            "flatten_trade_count": flatten_trades,
            "reason_counts": {} if reasons is None else reasons,
        },
    }


def _fold(
    *,
    fold: int,
    baseline: dict[str, object],
    session: dict[str, object],
    combined: dict[str, object],
) -> dict[str, object]:
    return {
        "fold": fold,
        "first_date": f"2026-08-{fold:02d}",
        "last_date": f"2026-08-{fold + 6:02d}",
        "candidate_metrics": {
            "CONDITIONAL_1_5X": baseline,
            "CONDITIONAL_SESSION_RISK": session,
            "CONDITIONAL_COMBINED_RISK": combined,
        },
    }


def test_walk_forward_audit_localizes_session_improvement_and_side_delta() -> None:
    inactive_baseline = _candidate(
        pnl=5.0,
        long_trades=3,
        long_pnl=8.0,
        short_trades=2,
        short_pnl=-3.0,
        enabled=False,
    )
    inactive_session = _candidate(
        pnl=5.0,
        long_trades=3,
        long_pnl=8.0,
        short_trades=2,
        short_pnl=-3.0,
        enabled=True,
    )
    active_baseline = _candidate(
        pnl=-9.0,
        long_trades=8,
        long_pnl=1.5,
        short_trades=6,
        short_pnl=-10.5,
        enabled=False,
    )
    active_session = _candidate(
        pnl=0.75,
        long_trades=7,
        long_pnl=1.5,
        short_trades=5,
        short_pnl=-0.75,
        enabled=True,
        risk_events=1,
        entry_blocks=23,
        reasons={"SESSION_CONSECUTIVE_LOSS_LIMIT_REACHED": 1},
    )
    report = {
        "folds": [
            _fold(
                fold=1,
                baseline=inactive_baseline,
                session=inactive_session,
                combined=_candidate(
                    pnl=5.0,
                    long_trades=3,
                    long_pnl=8.0,
                    short_trades=2,
                    short_pnl=-3.0,
                    enabled=True,
                ),
            ),
            _fold(
                fold=2,
                baseline=active_baseline,
                session=active_session,
                combined=_candidate(
                    pnl=0.80,
                    long_trades=7,
                    long_pnl=1.55,
                    short_trades=5,
                    short_pnl=-0.75,
                    enabled=True,
                    risk_events=1,
                    entry_blocks=23,
                    reasons={"SESSION_CONSECUTIVE_LOSS_LIMIT_REACHED": 1},
                ),
            ),
        ]
    }

    result = audit_walk_forward_session_risk(report)

    assert result["fold_count"] == 2
    assert result["session_risk_active_fold_count"] == 1
    assert result["session_risk_inactive_fold_count"] == 1
    assert result["session_risk_entry_block_count"] == 23
    assert result["session_risk_flatten_trade_count"] == 0
    assert result["session_risk_reason_counts"] == {
        "SESSION_CONSECUTIVE_LOSS_LIMIT_REACHED": 1
    }
    assert result["session_vs_baseline_total_net_pnl_delta_usdt"] == 9.75
    assert result["active_folds_session_vs_baseline_net_pnl_delta_usdt"] == 9.75
    assert result["inactive_folds_session_vs_baseline_net_pnl_delta_usdt"] == 0.0
    assert result["combined_minus_session_total_net_pnl_delta_usdt"] == 0.05
    assert result["side_deltas"]["LONG"] == {
        "closed_trade_delta": -1,
        "net_pnl_delta_usdt": 0.0,
    }
    assert result["side_deltas"]["SHORT"] == {
        "closed_trade_delta": -1,
        "net_pnl_delta_usdt": 9.75,
    }
    assert result["attribution_is_observational_not_causal"] is True
    assert result["session_threshold_retuning_allowed"] is False
    assert result["directional_filter_activation_allowed"] is False
    assert result["live_promotion_allowed"] is False


def test_walk_forward_audit_allows_noop_session_folds_without_inventing_effect() -> None:
    baseline = _candidate(
        pnl=12.0,
        long_trades=3,
        long_pnl=12.0,
        short_trades=0,
        short_pnl=0.0,
        enabled=False,
    )
    session = _candidate(
        pnl=12.0,
        long_trades=3,
        long_pnl=12.0,
        short_trades=0,
        short_pnl=0.0,
        enabled=True,
    )
    report = {
        "folds": [
            _fold(fold=1, baseline=baseline, session=session, combined=session),
        ]
    }

    result = audit_walk_forward_session_risk(report)

    assert result["session_risk_active_fold_count"] == 0
    assert result["session_risk_entry_block_count"] == 0
    assert result["session_vs_baseline_total_net_pnl_delta_usdt"] == 0.0
    assert result["combined_minus_session_total_net_pnl_delta_usdt"] == 0.0


def test_walk_forward_audit_fails_closed_when_required_candidate_is_missing() -> None:
    try:
        audit_walk_forward_session_risk({"folds": [{"fold": 1, "candidate_metrics": {}}]})
    except ValueError as exc:
        assert "CONDITIONAL_1_5X" in str(exc)
    else:
        raise AssertionError("walk-forward session-risk audit must reject missing baseline")
