from __future__ import annotations

from decimal import Decimal

import pytest

from app.strategy.crypto_historical_diagnostics import CryptoHistoricalTradeCondition
from app.strategy.crypto_signal_outcome_audit import (
    CryptoSignalOutcomeAuditPolicy,
    audit_crypto_signal_outcomes,
)


def _record(
    *,
    symbol: str,
    pnl: str,
    exit_reason: str = "NET_TARGET",
    side: str = "LONG",
    decision_time: str = "2026-08-01T00:00:00+00:00",
    quality: str = "1.50",
) -> CryptoHistoricalTradeCondition:
    return CryptoHistoricalTradeCondition(
        symbol=symbol,
        side=side,
        decision_time=decision_time,
        entry_time="2026-08-01T00:05:00+00:00",
        exit_time="2026-08-01T00:30:00+00:00",
        exit_reason=exit_reason,
        net_pnl_usdt=Decimal(pnl),
        maximum_favorable_r=Decimal("1.8"),
        maximum_adverse_r=Decimal("0.4"),
        holding_bars=5,
        quality_score=Decimal(quality),
        momentum_abs=Decimal("0.01"),
        momentum_to_atr=Decimal("1.5"),
        atr_fraction=Decimal("0.006"),
        trend_strength_atr=Decimal("1.2"),
        breakout_strength_atr=Decimal("0.3"),
        one_bar_atr_multiple=Decimal("0.8"),
        average_turnover_usdt=Decimal("1000000"),
        expected_net_edge_usd=Decimal("25"),
        exit_mode="FIXED_TARGET",
        volatility_regime="VOL_LOW_NORMAL",
        trend_regime="TREND_STRONG",
        breakout_regime="BREAKOUT_CONFIRMED",
        turnover_regime="TURNOVER_HIGH",
    )


def test_perfect_pattern_stays_retrospective_and_reports_uncertainty() -> None:
    rows = (
        _record(symbol="BTCUSDT", pnl="20"),
        _record(
            symbol="ETHUSDT",
            pnl="21",
            decision_time="2026-08-01T01:00:00+00:00",
        ),
        _record(
            symbol="BTCUSDT",
            pnl="19",
            decision_time="2026-08-01T02:00:00+00:00",
        ),
    )
    report = audit_crypto_signal_outcomes(
        rows,
        policy=CryptoSignalOutcomeAuditPolicy(
            minimum_pattern_trades=3,
            sample_sufficient_trades=5,
            minimum_cross_symbol_count=2,
        ),
    )

    assert report["retrospective_only"] is True
    assert report["strategy_promotion_allowed"] is False
    assert report["perfect_planned_profit_pattern_count"] == 1
    candidate = report["retrospective_perfect_planned_profit_cross_symbol_patterns"][0]
    assert candidate["observed_perfect_positive"] is True
    assert candidate["observed_perfect_planned_profit_exit"] is True
    assert candidate["sample_sufficient"] is False
    assert candidate["candidate_tier"] == "RETROSPECTIVE_PERFECT_PLANNED_SMALL_SAMPLE"
    assert candidate["positive_close_rate"] == 1.0
    assert 0.0 < candidate["positive_rate_wilson_lower_95"] < 1.0


def test_non_target_profit_is_not_hidden_inside_planned_profit_success() -> None:
    rows = (
        _record(symbol="BTCUSDT", pnl="20"),
        _record(
            symbol="ETHUSDT",
            pnl="4",
            exit_reason="BREAK_EVEN_STOP",
            decision_time="2026-08-01T01:00:00+00:00",
        ),
        _record(
            symbol="SOLUSDT",
            pnl="18",
            decision_time="2026-08-01T02:00:00+00:00",
        ),
    )
    report = audit_crypto_signal_outcomes(
        rows,
        policy=CryptoSignalOutcomeAuditPolicy(
            minimum_pattern_trades=3,
            sample_sufficient_trades=3,
            minimum_cross_symbol_count=2,
        ),
    )

    pattern = report["cross_symbol_patterns"][0]
    assert pattern["observed_perfect_positive"] is True
    assert pattern["observed_perfect_planned_profit_exit"] is False
    assert pattern["planned_profit_exit_rate"] == pytest.approx(2 / 3)
    assert pattern["exit_reason_counts"] == {"BREAK_EVEN_STOP": 1, "NET_TARGET": 2}


def test_one_loss_breaks_perfect_positive_pattern() -> None:
    rows = (
        _record(symbol="BTCUSDT", pnl="20"),
        _record(
            symbol="ETHUSDT",
            pnl="-10",
            exit_reason="HARD_STOP",
            decision_time="2026-08-01T01:00:00+00:00",
        ),
        _record(
            symbol="SOLUSDT",
            pnl="18",
            decision_time="2026-08-01T02:00:00+00:00",
        ),
    )
    report = audit_crypto_signal_outcomes(
        rows,
        policy=CryptoSignalOutcomeAuditPolicy(
            minimum_pattern_trades=3,
            sample_sufficient_trades=3,
            minimum_cross_symbol_count=2,
        ),
    )

    pattern = report["cross_symbol_patterns"][0]
    assert pattern["observed_perfect_positive"] is False
    assert pattern["positive_close_rate"] == pytest.approx(2 / 3)
    assert pattern["candidate_tier"] == "RETROSPECTIVE_MIXED"
    assert report["perfect_positive_pattern_count"] == 0


def test_numerical_dust_is_breakeven_not_a_win_or_perfect_pattern() -> None:
    rows = (
        _record(symbol="BTCUSDT", pnl="0.00000000000000000000000017", exit_reason="BREAK_EVEN_STOP"),
        _record(
            symbol="ETHUSDT",
            pnl="0.00000000000000000000000030",
            exit_reason="BREAK_EVEN_STOP",
            decision_time="2026-08-01T01:00:00+00:00",
        ),
        _record(
            symbol="SOLUSDT",
            pnl="0.0000004",
            exit_reason="BREAK_EVEN_STOP",
            decision_time="2026-08-01T02:00:00+00:00",
        ),
    )
    report = audit_crypto_signal_outcomes(
        rows,
        policy=CryptoSignalOutcomeAuditPolicy(
            minimum_pattern_trades=3,
            sample_sufficient_trades=3,
            minimum_cross_symbol_count=2,
        ),
    )

    aggregate = report["aggregate"]
    assert report["audit"] == "BYBIT_CRYPTO_SIGNAL_OUTCOME_AUDIT_V2"
    assert report["pnl_epsilon_usdt"] == 0.000001
    assert aggregate["positive_close_count"] == 0
    assert aggregate["breakeven_close_count"] == 3
    assert aggregate["loss_close_count"] == 0
    assert aggregate["positive_close_rate"] == 0.0
    assert report["perfect_positive_pattern_count"] == 0
    assert all(row["economic_outcome"] == "BREAKEVEN" for row in report["trade_rows"])


def test_trade_rows_expose_signal_clarity_and_excursions() -> None:
    report = audit_crypto_signal_outcomes((_record(symbol="BTCUSDT", pnl="20"),))
    trade = report["trade_rows"][0]

    assert trade["quality_score"] == 1.5
    assert trade["quality_ratio_to_entry_gate"] > 1.0
    assert trade["quality_margin_above_entry_gate"] == pytest.approx(0.4)
    assert trade["maximum_favorable_r"] == 1.8
    assert trade["maximum_adverse_r"] == 0.4
    assert trade["economic_outcome"] == "WIN"
    assert trade["planned_profit_exit"] is True


def test_policy_rejects_inverted_sample_thresholds() -> None:
    with pytest.raises(ValueError, match="minimum trades"):
        CryptoSignalOutcomeAuditPolicy(
            minimum_pattern_trades=10,
            sample_sufficient_trades=5,
        ).validate()
