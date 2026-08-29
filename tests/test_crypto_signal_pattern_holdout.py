from __future__ import annotations

from app.strategy.crypto_signal_pattern_holdout import (
    CryptoSignalPatternHoldoutPolicy,
    validate_crypto_signal_pattern_holdout,
)

_PATTERN = "LONG|VOL_LOW_NORMAL|TREND_MODERATE|BREAKOUT_PULLBACK|TURNOVER_LOW"


def _row(
    *,
    symbol: str,
    index: int,
    positive: bool = True,
    planned: bool = True,
    pattern: str = _PATTERN,
) -> dict[str, object]:
    return {
        "pattern": pattern,
        "symbol": symbol,
        "side": "LONG",
        "decision_time": f"2026-08-{index + 1:02d}T00:00:00+00:00",
        "positive_close": positive,
        "planned_profit_exit": planned,
        "net_pnl_usdt": 20.0 if positive else -10.0,
        "exit_reason": (
            "NET_TARGET"
            if planned
            else ("BREAK_EVEN_STOP" if positive else "HARD_STOP")
        ),
    }


def _supported_rows(
    *,
    positive: bool = True,
    planned: bool = True,
) -> list[dict[str, object]]:
    return [
        _row(
            symbol="BTCUSDT" if index < 3 else "ETHUSDT",
            index=index,
            positive=positive,
            planned=planned,
        )
        for index in range(5)
    ]


def test_discovery_perfect_pattern_must_repeat_in_holdout() -> None:
    report = validate_crypto_signal_pattern_holdout(
        _supported_rows(),
        _supported_rows(),
    )

    assert report["candidate_count"] == 1
    assert report["observed_holdout_perfect_count"] == 1
    candidate = report["candidates"][0]
    assert candidate["status"] == "OBSERVED_HOLDOUT_PERFECT_PLANNED_PROFIT"
    assert candidate["discovery"]["positive_rate_wilson_lower_95"] < 1.0
    assert report["strategy_promotion_allowed"] is False
    assert report["prospective_confirmation_required"] is True


def test_one_holdout_loss_breaks_historical_perfect_status() -> None:
    holdout = _supported_rows()
    holdout[-1] = _row(symbol="ETHUSDT", index=4, positive=False, planned=False)
    report = validate_crypto_signal_pattern_holdout(_supported_rows(), holdout)

    assert report["observed_holdout_perfect_count"] == 0
    candidate = report["candidates"][0]
    assert candidate["status"] == "HOLDOUT_BROKE_PERFECT_HISTORY"
    assert candidate["holdout"]["positive_close_rate"] == 0.8


def test_small_discovery_perfect_sample_is_not_a_candidate() -> None:
    discovery = _supported_rows()[:4]
    report = validate_crypto_signal_pattern_holdout(discovery, _supported_rows())

    assert report["candidate_count"] == 0
    assert report["observed_holdout_perfect_count"] == 0


def test_single_symbol_discovery_is_not_cross_token_evidence() -> None:
    discovery = [_row(symbol="BTCUSDT", index=index) for index in range(5)]
    report = validate_crypto_signal_pattern_holdout(discovery, _supported_rows())

    assert report["candidate_count"] == 0


def test_positive_but_nonplanned_closes_do_not_claim_planned_profit_perfection() -> None:
    discovery = _supported_rows(planned=False)
    holdout = _supported_rows(planned=False)
    report = validate_crypto_signal_pattern_holdout(discovery, holdout)

    candidate = report["candidates"][0]
    assert candidate["status"] == "OBSERVED_HOLDOUT_PERFECT_POSITIVE"
    assert candidate["discovery_perfect_planned_profit"] is False
    assert candidate["holdout_perfect_planned_profit"] is False


def test_policy_can_raise_support_without_fitting_signal_thresholds() -> None:
    report = validate_crypto_signal_pattern_holdout(
        _supported_rows(),
        _supported_rows(),
        policy=CryptoSignalPatternHoldoutPolicy(
            minimum_discovery_trades=6,
            minimum_holdout_trades=6,
            minimum_discovery_symbols=2,
            minimum_holdout_symbols=2,
        ),
    )

    assert report["candidate_count"] == 0
    assert report["pattern_thresholds_fitted"] is False
    assert report["quality_threshold_retuned"] is False
