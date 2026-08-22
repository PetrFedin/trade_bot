from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.marketdata.bybit_derivatives_history import (
    BybitAccountRatioPoint,
    BybitDerivativesHistory,
    BybitHistoricalFundingPoint,
    BybitOpenInterestPoint,
)
from app.strategy.crypto_derivatives_context import (
    build_crypto_trade_derivatives_context,
    diagnose_crypto_derivatives_context,
)

_START = datetime(2026, 1, 1, tzinfo=UTC)


def _ms(minutes: int) -> int:
    return int((_START + timedelta(minutes=minutes)).timestamp() * 1000)


def _history(symbol: str = "BTCUSDT") -> BybitDerivativesHistory:
    return BybitDerivativesHistory(
        symbol=symbol,
        start_ms=_ms(0),
        end_ms=_ms(60),
        interval="5min",
        open_interest=(
            BybitOpenInterestPoint(symbol, _ms(5), Decimal("100"), Decimal("50")),
            BybitOpenInterestPoint(symbol, _ms(10), Decimal("110"), Decimal("55")),
            BybitOpenInterestPoint(symbol, _ms(20), Decimal("120"), Decimal("60")),
        ),
        account_ratio=(
            BybitAccountRatioPoint(symbol, _ms(5), Decimal("0.50"), Decimal("0.50")),
            BybitAccountRatioPoint(symbol, _ms(10), Decimal("0.56"), Decimal("0.44")),
            BybitAccountRatioPoint(symbol, _ms(20), Decimal("0.70"), Decimal("0.30")),
        ),
        funding=(
            BybitHistoricalFundingPoint(symbol, _ms(5), Decimal("0.0001")),
            BybitHistoricalFundingPoint(symbol, _ms(17), Decimal("0.0002")),
            BybitHistoricalFundingPoint(symbol, _ms(30), Decimal("-0.0001")),
        ),
        request_count=3,
        host="api.bybit.com",
    )


def _replay() -> dict[str, object]:
    decision = (_START + timedelta(minutes=12)).isoformat()
    entry = (_START + timedelta(minutes=15)).isoformat()
    exit_time = (_START + timedelta(minutes=25)).isoformat()
    return {
        "closed_trades": [
            {
                "symbol": "BTCUSDT",
                "side": "LONG",
                "decision_time": decision,
                "entry_time": entry,
                "exit_time": exit_time,
                "exit_reason": "NET_TARGET",
                "net_pnl_usdt": 12,
                "maximum_favorable_r_before_exit": 2.0,
                "maximum_adverse_r_before_exit": -0.2,
            }
        ],
        "decision_events": [
            {
                "event": "ENTRY",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "decision_time": decision,
                "execution_time": entry,
            }
        ],
        "strategy_promotion_allowed": False,
        "bybit_live_order_routing_allowed": False,
    }


def test_context_uses_only_points_at_or_before_decision_and_separates_holding_funding() -> None:
    contexts = build_crypto_trade_derivatives_context(
        _replay(),
        {"BTCUSDT": _history()},
    )

    assert len(contexts) == 1
    context = contexts[0]
    assert context.open_interest_timestamp_ms == _ms(10)
    assert context.open_interest == Decimal("110")
    assert context.previous_open_interest == Decimal("100")
    assert context.open_interest_delta == Decimal("10")
    assert context.open_interest_delta_fraction == Decimal("0.1")
    assert context.account_ratio_timestamp_ms == _ms(10)
    assert context.long_account_ratio == Decimal("0.56")
    assert context.short_account_ratio == Decimal("0.44")
    assert context.long_short_account_ratio == Decimal("0.56") / Decimal("0.44")
    assert context.prior_funding_timestamp_ms == _ms(5)
    assert context.prior_funding_rate == Decimal("0.0001")
    assert context.holding_funding_event_count == 1
    assert context.holding_funding_rate_sum == Decimal("0.0002")
    assert context.decision_context_complete is True
    assert context.missing_reasons == ()
    assert context.open_interest_regime == "OI_RISING"
    assert context.crowding_regime == "LONG_HEAVY"
    assert context.prior_funding_regime == "FUNDING_POSITIVE"
    assert _ms(20) > int(datetime.fromisoformat(context.decision_time).timestamp() * 1000)


def test_missing_history_is_explicit_instead_of_fabricated() -> None:
    contexts = build_crypto_trade_derivatives_context(_replay(), {})
    context = contexts[0]
    assert context.decision_context_complete is False
    assert context.missing_reasons == ("DERIVATIVES_HISTORY_MISSING",)
    assert context.open_interest is None
    assert context.long_account_ratio is None
    assert context.prior_funding_rate is None


def test_incomplete_warmup_marks_missing_previous_oi_and_prior_funding() -> None:
    symbol = "BTCUSDT"
    history = BybitDerivativesHistory(
        symbol=symbol,
        start_ms=_ms(10),
        end_ms=_ms(60),
        interval="5min",
        open_interest=(
            BybitOpenInterestPoint(symbol, _ms(10), Decimal("110"), None),
        ),
        account_ratio=(
            BybitAccountRatioPoint(symbol, _ms(10), Decimal("0.50"), Decimal("0.50")),
        ),
        funding=(),
        request_count=3,
        host="api.bybit.com",
    )
    context = build_crypto_trade_derivatives_context(
        _replay(),
        {symbol: history},
    )[0]
    assert context.decision_context_complete is False
    assert "OPEN_INTEREST_PREVIOUS_POINT_MISSING" in context.missing_reasons
    assert "PRIOR_FUNDING_RATE_MISSING" in context.missing_reasons


def test_diagnostics_group_point_in_time_states_without_promoting_strategy() -> None:
    context = build_crypto_trade_derivatives_context(
        _replay(),
        {"BTCUSDT": _history()},
    )[0]
    report = diagnose_crypto_derivatives_context(
        (context,),
        minimum_pattern_trades=1,
    )
    assert report["trade_count"] == 1
    assert report["complete_decision_context_count"] == 1
    assert report["complete_decision_context_fraction"] == 1.0
    assert report["by_open_interest_regime"]["OI_RISING"]["win_count"] == 1
    assert report["by_crowding_regime"]["LONG_HEAVY"]["win_count"] == 1
    assert report["by_prior_funding_regime"]["FUNDING_POSITIVE"]["win_count"] == 1
    assert report["repeated_patterns"][0]["sample_sufficient"] is True
    assert report["funding_dollar_cost_reconciled"] is False
    assert report["parameter_retuning_performed"] is False
    assert report["strategy_promotion_allowed"] is False
    assert report["live_activation_allowed"] is False
    assert report["bybit_live_order_routing_allowed"] is False
    assert report["causal_claim_allowed"] is False
    assert report["predictive_guarantee_allowed"] is False


def test_context_rejects_live_capable_replay_and_out_of_range_history() -> None:
    replay = _replay()
    replay["bybit_live_order_routing_allowed"] = True
    with pytest.raises(ValueError, match="bybit_live_order_routing_allowed=false"):
        build_crypto_trade_derivatives_context(replay, {"BTCUSDT": _history()})

    history = _history()
    shortened = BybitDerivativesHistory(
        symbol=history.symbol,
        start_ms=history.start_ms,
        end_ms=_ms(20),
        interval=history.interval,
        open_interest=history.open_interest,
        account_ratio=history.account_ratio,
        funding=history.funding[:2],
        request_count=history.request_count,
        host=history.host,
    )
    with pytest.raises(ValueError, match="exit falls outside"):
        build_crypto_trade_derivatives_context(
            _replay(),
            {"BTCUSDT": shortened},
        )
