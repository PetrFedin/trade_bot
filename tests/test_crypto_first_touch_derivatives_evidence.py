from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.marketdata.bybit_derivatives_history import (
    BybitAccountRatioPoint,
    BybitDerivativesHistory,
    BybitHistoricalFundingPoint,
    BybitOpenInterestPoint,
)
from app.strategy.crypto_first_touch_derivatives_evidence import (
    CryptoFirstTouchDerivativesEvidencePolicy,
    build_crypto_first_touch_derivatives_evidence,
    diagnose_crypto_first_touch_derivatives_evidence,
)


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _history(symbol: str, start: datetime) -> BybitDerivativesHistory:
    oi = tuple(
        BybitOpenInterestPoint(
            symbol=symbol,
            timestamp_ms=_ms(start + timedelta(hours=index)),
            open_interest=Decimal("1000") + Decimal(index * 20),
            single_open_interest=None,
        )
        for index in range(10)
    )
    ratios = tuple(
        BybitAccountRatioPoint(
            symbol=symbol,
            timestamp_ms=_ms(start + timedelta(hours=index)),
            buy_ratio=Decimal("0.50"),
            sell_ratio=Decimal("0.50"),
        )
        for index in range(10)
    )
    funding = tuple(
        BybitHistoricalFundingPoint(
            symbol=symbol,
            timestamp_ms=_ms(start + timedelta(hours=index * 2)),
            funding_rate=Decimal("0.0001"),
        )
        for index in range(5)
    )
    history = BybitDerivativesHistory(
        symbol=symbol,
        start_ms=_ms(start),
        end_ms=_ms(start + timedelta(hours=12)),
        interval="1h",
        open_interest=oi,
        account_ratio=ratios,
        funding=funding,
        request_count=3,
        host="api.bybit.com",
    )
    history.validate()
    return history


def _row(
    symbol: str,
    *,
    day_offset: int,
    first_touch_state: str = "TARGET_FIRST",
) -> dict[str, object]:
    base = datetime(2026, 8, 20, 3, tzinfo=UTC) + timedelta(days=day_offset)
    return {
        "symbol": symbol,
        "side": "LONG",
        "decision_time": base.isoformat(),
        "signal_available_at": (base + timedelta(minutes=5)).isoformat(),
        "utc_day": base.date().isoformat(),
        "first_touch_state": first_touch_state,
        "pattern": "LONG|STRONG|VOL_LOW_NORMAL|TREND_STRONG|BREAKOUT_CONFIRMED",
        "quality_score": 4.0,
        "quality_ratio_to_entry_gate": 3.5,
        "momentum_to_atr": 2.0,
        "trend_strength_atr": 1.2,
        "breakout_strength_atr": 0.3,
        "atr_fraction": 0.01,
        "one_bar_atr_multiple": 0.5,
        "average_turnover_usdt": 1_000_000 + day_offset,
        "expected_net_edge_usd": 24.0,
        "maximum_favorable_r": 2.5,
        "maximum_adverse_r": 0.5,
    }


def _histories_for_rows(rows: list[dict[str, object]]) -> dict[str, BybitDerivativesHistory]:
    result: dict[str, BybitDerivativesHistory] = {}
    for symbol in {str(row["symbol"]) for row in rows}:
        first = min(
            datetime.fromisoformat(str(row["decision_time"]))
            for row in rows
            if row["symbol"] == symbol
        )
        result[symbol] = _history(symbol, first - timedelta(hours=3))
    return result


def test_derivatives_join_uses_only_points_at_or_before_decision() -> None:
    rows = [_row("BTCUSDT", day_offset=0)]
    history = _histories_for_rows(rows)
    evidence = build_crypto_first_touch_derivatives_evidence(rows, history)

    assert len(evidence) == 1
    item = evidence[0]
    decision_ms = _ms(datetime.fromisoformat(str(rows[0]["decision_time"])))
    assert item.open_interest_timestamp_ms is not None
    assert item.account_ratio_timestamp_ms is not None
    assert item.prior_funding_timestamp_ms is not None
    assert item.open_interest_timestamp_ms <= decision_ms
    assert item.account_ratio_timestamp_ms <= decision_ms
    assert item.prior_funding_timestamp_ms <= decision_ms
    assert item.open_interest_regime == "OI_RISING"
    assert item.crowding_regime == "BALANCED"
    assert item.prior_funding_regime == "FUNDING_POSITIVE"
    assert item.decision_context_complete is True


def test_cross_token_perfect_cell_requires_symbol_and_day_support() -> None:
    rows = [
        _row("BTCUSDT", day_offset=0),
        _row("ETHUSDT", day_offset=0),
        _row("BTCUSDT", day_offset=1),
        _row("ETHUSDT", day_offset=1),
        _row("BTCUSDT", day_offset=2),
        _row("ETHUSDT", day_offset=2),
    ]
    evidence = build_crypto_first_touch_derivatives_evidence(
        rows,
        _histories_for_rows(rows),
    )
    report = diagnose_crypto_first_touch_derivatives_evidence(
        evidence,
        policy=CryptoFirstTouchDerivativesEvidencePolicy(
            minimum_cell_episodes=5,
            sample_sufficient_episodes=30,
            minimum_cross_symbol_count=2,
            minimum_distinct_days=3,
        ),
    )

    assert report["episode_count"] == 6
    assert report["complete_context_count"] == 6
    assert report["perfect_cross_token_cell_count"] == 1
    candidate = report["retrospective_perfect_cross_token_cells"][0]
    assert candidate["episode_count"] == 6
    assert candidate["symbol_count"] == 2
    assert candidate["distinct_day_count"] == 3
    assert candidate["target_first_rate"] == 1.0
    assert candidate["sample_sufficient"] is False
    assert 0 < candidate["target_first_wilson_lower_95"] < 1
    assert report["strategy_promotion_allowed"] is False
    assert report["live_activation_allowed"] is False


def test_one_stop_first_rejects_historical_perfect_cell() -> None:
    rows = [
        _row("BTCUSDT", day_offset=0),
        _row("ETHUSDT", day_offset=0),
        _row("BTCUSDT", day_offset=1),
        _row("ETHUSDT", day_offset=1),
        _row("BTCUSDT", day_offset=2),
        _row("ETHUSDT", day_offset=2, first_touch_state="STOP_FIRST"),
    ]
    evidence = build_crypto_first_touch_derivatives_evidence(
        rows,
        _histories_for_rows(rows),
    )
    report = diagnose_crypto_first_touch_derivatives_evidence(evidence)

    assert report["perfect_cross_token_cell_count"] == 0
    qualified = report["qualified_cross_token_cells"]
    assert qualified
    assert qualified[0]["target_first_count"] == 5
    assert qualified[0]["stop_first_count"] == 1


def test_missing_derivatives_history_stays_unknown_and_unqualified() -> None:
    rows = [_row("BTCUSDT", day_offset=0)]
    evidence = build_crypto_first_touch_derivatives_evidence(rows, {})
    item = evidence[0]

    assert item.decision_context_complete is False
    assert item.open_interest_regime == "OI_UNKNOWN"
    assert item.crowding_regime == "CROWDING_UNKNOWN"
    assert item.prior_funding_regime == "FUNDING_UNKNOWN"
    assert item.stress_regime == "STRESS_UNKNOWN"
    report = diagnose_crypto_first_touch_derivatives_evidence(evidence)
    assert report["complete_context_count"] == 0
    assert report["qualified_cross_token_cells"] == []
