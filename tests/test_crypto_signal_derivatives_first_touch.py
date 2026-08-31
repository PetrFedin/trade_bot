from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.marketdata.bybit_derivatives_history import (
    BybitAccountRatioPoint,
    BybitDerivativesHistory,
    BybitHistoricalFundingPoint,
    BybitOpenInterestPoint,
)
from app.strategy.crypto_signal_derivatives_first_touch import (
    CryptoSignalDerivativesFirstTouchPolicy,
    audit_crypto_signal_derivatives_first_touch,
)

_START = datetime(2026, 8, 1, 12, tzinfo=UTC)
_PRICE_PATTERN = "LONG|STRONG|VOL_LOW_NORMAL|TREND_MODERATE|BREAKOUT_PULLBACK"


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _outcome_row(
    symbol: str,
    *,
    day: int,
    minute: int = 0,
    state: str = "TARGET_FIRST",
) -> dict[str, object]:
    decision = _START + timedelta(days=day, minutes=minute)
    available = decision + timedelta(minutes=5)
    touch = available + timedelta(minutes=15)
    return {
        "symbol": symbol,
        "side": "LONG",
        "decision_time": decision.isoformat(),
        "signal_available_at": available.isoformat(),
        "utc_day": available.date().isoformat(),
        "quality_score": 4.0,
        "quality_ratio_to_entry_gate": 4.0,
        "clarity_band": "STRONG",
        "momentum_to_atr": 2.0,
        "trend_strength_atr": 0.8,
        "breakout_strength_atr": -0.2,
        "atr_fraction": 0.006,
        "one_bar_atr_multiple": 0.5,
        "average_turnover_usdt": 1_000_000.0,
        "expected_net_edge_usd": 24.0,
        "first_touch_state": state,
        "first_touch_bar": None if state == "NEITHER" else touch.isoformat(),
        "maximum_favorable_r": 2.5,
        "maximum_adverse_r": 0.7,
        "modeled_stop_net_pnl_usdt": -10.0,
        "pattern": _PRICE_PATTERN,
    }


def _report(rows: list[dict[str, object]]) -> dict[str, object]:
    return {
        "audit": "BYBIT_CRYPTO_PLAN_ELIGIBLE_FIRST_TOUCH_V2",
        "outcome_rows": rows,
        "retrospective_only": True,
        "counterfactual_portfolio_pnl_claim_allowed": False,
        "strategy_selection_allowed": False,
        "strategy_promotion_allowed": False,
        "trade_actionable": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
        "predictive_guarantee_allowed": False,
    }


def _history(
    symbol: str,
    *,
    days: int = 10,
    include_future_extreme: bool = False,
) -> BybitDerivativesHistory:
    open_interest: list[BybitOpenInterestPoint] = []
    ratios: list[BybitAccountRatioPoint] = []
    funding: list[BybitHistoricalFundingPoint] = []
    for day in range(-1, days + 1):
        timestamp = _START + timedelta(days=day, hours=-1)
        open_interest.append(
            BybitOpenInterestPoint(
                symbol,
                _ms(timestamp),
                Decimal("100") + Decimal(day + 2) * Decimal("3"),
                None,
            )
        )
        ratios.append(
            BybitAccountRatioPoint(
                symbol,
                _ms(timestamp),
                Decimal("0.50"),
                Decimal("0.50"),
            )
        )
        funding.append(
            BybitHistoricalFundingPoint(
                symbol,
                _ms(timestamp),
                Decimal("0.0001"),
            )
        )
    if include_future_extreme:
        future = _START + timedelta(minutes=1)
        open_interest.append(
            BybitOpenInterestPoint(symbol, _ms(future), Decimal("10000"), None)
        )
        ratios.append(
            BybitAccountRatioPoint(
                symbol,
                _ms(future),
                Decimal("0.90"),
                Decimal("0.10"),
            )
        )
        funding.append(
            BybitHistoricalFundingPoint(symbol, _ms(future), Decimal("-0.01"))
        )
        open_interest.sort(key=lambda item: item.timestamp_ms)
        ratios.sort(key=lambda item: item.timestamp_ms)
        funding.sort(key=lambda item: item.timestamp_ms)
    return BybitDerivativesHistory(
        symbol=symbol,
        start_ms=_ms(_START - timedelta(days=2)),
        end_ms=_ms(_START + timedelta(days=days + 2)),
        interval="1h",
        open_interest=tuple(open_interest),
        account_ratio=tuple(ratios),
        funding=tuple(funding),
        request_count=3,
        host="api.bybit.com",
    )


def _policy(*, sample_sufficient: int = 5) -> CryptoSignalDerivativesFirstTouchPolicy:
    return CryptoSignalDerivativesFirstTouchPolicy(
        minimum_pattern_observations=3,
        sample_sufficient_observations=sample_sufficient,
        minimum_cross_symbol_count=2,
        minimum_distinct_days=3,
    )


def test_join_uses_only_derivatives_known_at_or_before_signal_decision() -> None:
    row = _outcome_row("BTCUSDT", day=0)
    baseline = audit_crypto_signal_derivatives_first_touch(
        _report([row]),
        {"BTCUSDT": _history("BTCUSDT")},
        policy=_policy(),
    )
    with_future = audit_crypto_signal_derivatives_first_touch(
        _report([row]),
        {"BTCUSDT": _history("BTCUSDT", include_future_extreme=True)},
        policy=_policy(),
    )

    baseline_row = baseline["raw_rows"][0]
    future_row = with_future["raw_rows"][0]
    for field in (
        "open_interest_regime",
        "crowding_regime",
        "prior_funding_regime",
        "stress_regime",
        "stress_score",
        "open_interest_timestamp_ms",
        "open_interest_delta_fraction",
        "account_ratio_timestamp_ms",
        "long_account_ratio",
        "prior_funding_timestamp_ms",
        "prior_funding_rate",
        "enriched_pattern",
        "exact_cell_key",
    ):
        assert future_row[field] == baseline_row[field]
    decision_ms = _ms(_START)
    assert baseline_row["open_interest_timestamp_ms"] <= decision_ms
    assert baseline_row["account_ratio_timestamp_ms"] <= decision_ms
    assert baseline_row["prior_funding_timestamp_ms"] <= decision_ms


def test_incomplete_derivatives_context_is_explicit_and_not_used_for_candidates() -> None:
    symbol = "BTCUSDT"
    row = _outcome_row(symbol, day=0)
    history = BybitDerivativesHistory(
        symbol=symbol,
        start_ms=_ms(_START - timedelta(days=1)),
        end_ms=_ms(_START + timedelta(days=1)),
        interval="1h",
        open_interest=(
            BybitOpenInterestPoint(
                symbol,
                _ms(_START - timedelta(hours=1)),
                Decimal("100"),
                None,
            ),
        ),
        account_ratio=(
            BybitAccountRatioPoint(
                symbol,
                _ms(_START - timedelta(hours=1)),
                Decimal("0.50"),
                Decimal("0.50"),
            ),
        ),
        funding=(),
        request_count=3,
        host="api.bybit.com",
    )

    report = audit_crypto_signal_derivatives_first_touch(
        _report([row]),
        {symbol: history},
        policy=_policy(),
    )

    enriched = report["episode_rows"][0]
    assert enriched["derivatives_context_complete"] is False
    assert enriched["stress_regime"] == "STRESS_UNKNOWN"
    assert "OPEN_INTEREST_PREVIOUS_POINT_MISSING" in enriched["derivatives_missing_reasons"]
    assert "PRIOR_FUNDING_RATE_MISSING" in enriched["derivatives_missing_reasons"]
    assert report["complete_derivatives_episode_count"] == 0
    assert report["qualified_transferable_pattern_rows"] == []
    assert report["qualified_exact_cell_rows"] == []


def test_transferable_perfect_pattern_requires_multi_symbol_multi_day_support() -> None:
    rows = [
        _outcome_row("BTCUSDT", day=0),
        _outcome_row("ETHUSDT", day=0),
        _outcome_row("BTCUSDT", day=1),
        _outcome_row("ETHUSDT", day=1),
        _outcome_row("BTCUSDT", day=2),
        _outcome_row("ETHUSDT", day=2),
    ]
    report = audit_crypto_signal_derivatives_first_touch(
        _report(rows),
        {"BTCUSDT": _history("BTCUSDT"), "ETHUSDT": _history("ETHUSDT")},
        policy=_policy(sample_sufficient=5),
    )

    assert report["independent_episode_count"] == 6
    assert report["perfect_transferable_pattern_count"] == 1
    candidate = report["retrospective_perfect_transferable_patterns"][0]
    assert candidate["observation_count"] == 6
    assert candidate["target_first_count"] == 6
    assert candidate["symbol_count"] == 2
    assert candidate["distinct_day_count"] == 3
    assert candidate["sample_sufficient"] is True
    assert candidate["candidate_tier"] == "RETROSPECTIVE_PERFECT_SAMPLE_SUFFICIENT"
    assert report["retrospective_only"] is True
    assert report["strategy_selection_allowed"] is False
    assert report["strategy_promotion_allowed"] is False
    assert report["trade_actionable"] is False
    assert report["demo_activation_allowed"] is False
    assert report["live_activation_allowed"] is False


def test_exact_cell_becomes_oos_ready_only_after_predeclared_sample_threshold() -> None:
    rows = [
        _outcome_row("BTCUSDT", day=0),
        _outcome_row("BTCUSDT", day=1),
        _outcome_row("BTCUSDT", day=2),
        _outcome_row("BTCUSDT", day=3),
    ]
    histories = {"BTCUSDT": _history("BTCUSDT")}
    below = audit_crypto_signal_derivatives_first_touch(
        _report(rows[:3]),
        histories,
        policy=_policy(sample_sufficient=4),
    )
    enough = audit_crypto_signal_derivatives_first_touch(
        _report(rows),
        histories,
        policy=_policy(sample_sufficient=4),
    )

    assert below["perfect_exact_cell_count"] == 1
    assert below["oos_ready_retrospective_exact_cell_count"] == 0
    assert enough["perfect_exact_cell_count"] == 1
    assert enough["oos_ready_retrospective_exact_cell_count"] == 1
    candidate = enough["oos_ready_retrospective_exact_cells"][0]
    assert candidate["observation_count"] == 4
    assert candidate["distinct_day_count"] == 4
    assert candidate["sample_sufficient"] is True


def test_one_counterexample_rejects_retrospective_perfect_hypothesis() -> None:
    rows = [
        _outcome_row("BTCUSDT", day=0),
        _outcome_row("ETHUSDT", day=0),
        _outcome_row("BTCUSDT", day=1),
        _outcome_row("ETHUSDT", day=1),
        _outcome_row("BTCUSDT", day=2),
        _outcome_row("ETHUSDT", day=2, state="STOP_FIRST"),
    ]
    report = audit_crypto_signal_derivatives_first_touch(
        _report(rows),
        {"BTCUSDT": _history("BTCUSDT"), "ETHUSDT": _history("ETHUSDT")},
        policy=_policy(sample_sufficient=5),
    )

    assert report["perfect_transferable_pattern_count"] == 0
    candidate = report["qualified_transferable_pattern_rows"][0]
    assert candidate["target_first_count"] == 5
    assert candidate["stop_first_count"] == 1
    assert candidate["observed_perfect_target_first"] is False


def test_boundary_rejects_tampered_trade_actionable_first_touch_report() -> None:
    source = _report([_outcome_row("BTCUSDT", day=0)])
    tampered = deepcopy(source)
    tampered["trade_actionable"] = True

    with pytest.raises(ValueError, match="trade_actionable=False"):
        audit_crypto_signal_derivatives_first_touch(
            tampered,
            {"BTCUSDT": _history("BTCUSDT")},
            policy=_policy(),
        )
