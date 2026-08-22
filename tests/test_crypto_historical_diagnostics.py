from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.marketdata.bybit_v5 import BybitKlineAcquisition, BybitKlineBar
from app.strategy.crypto_historical_diagnostics import (
    CryptoHistoricalDiagnosticsPolicy,
    diagnose_crypto_historical_conditions,
)

_START = datetime(2026, 1, 1, tzinfo=UTC)


def _trend_bars(symbol: str, *, long: bool) -> tuple[BybitKlineBar, ...]:
    bars: list[BybitKlineBar] = []
    for index in range(36):
        base = Decimal("100") + Decimal(index) if long else Decimal("200") - Decimal(index)
        previous = (
            Decimal("99") + Decimal(index)
            if long
            else Decimal("201") - Decimal(index)
        )
        open_price = previous
        close_price = base
        high = max(open_price, close_price) + Decimal("0.4")
        low = min(open_price, close_price) - Decimal("0.4")
        bars.append(
            BybitKlineBar(
                symbol=symbol,
                start_time=_START + timedelta(minutes=5 * index),
                open=open_price,
                high=high,
                low=low,
                close=close_price,
                volume=Decimal("10000"),
                turnover=Decimal("2000000") + Decimal(index * 1000),
            )
        )
    return tuple(bars)


def _acquisition() -> BybitKlineAcquisition:
    btc = _trend_bars("BTCUSDT", long=True)
    eth = _trend_bars("ETHUSDT", long=False)
    return BybitKlineAcquisition(
        bars=tuple(sorted((*btc, *eth), key=lambda bar: (bar.symbol, bar.start_time))),
        pages_by_symbol={"BTCUSDT": 1, "ETHUSDT": 1},
    )


def _trade(
    *,
    symbol: str,
    side: str,
    decision_index: int,
    entry_index: int,
    net_pnl: str,
    exit_reason: str,
    mfe: str,
    mae: str,
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "side": side,
        "decision_time": (_START + timedelta(minutes=5 * decision_index)).isoformat(),
        "entry_time": (_START + timedelta(minutes=5 * entry_index)).isoformat(),
        "exit_time": (_START + timedelta(minutes=5 * (entry_index + 2))).isoformat(),
        "net_pnl_usdt": float(Decimal(net_pnl)),
        "exit_reason": exit_reason,
        "maximum_favorable_r_before_exit": float(Decimal(mfe)),
        "maximum_adverse_r_before_exit": float(Decimal(mae)),
        "holding_bars": 2,
    }


def _entry_event(
    *,
    symbol: str,
    side: str,
    decision_index: int,
    entry_index: int,
    expected_edge: str,
    exit_mode: str,
) -> dict[str, object]:
    return {
        "event": "ENTRY",
        "symbol": symbol,
        "side": side,
        "decision_time": (_START + timedelta(minutes=5 * decision_index)).isoformat(),
        "execution_time": (_START + timedelta(minutes=5 * entry_index)).isoformat(),
        "expected_net_edge_usd": float(Decimal(expected_edge)),
        "exit_mode": exit_mode,
    }


def _replay() -> dict[str, object]:
    return {
        "closed_trades": [
            _trade(
                symbol="BTCUSDT",
                side="LONG",
                decision_index=34,
                entry_index=35,
                net_pnl="12",
                exit_reason="NET_TARGET",
                mfe="2.4",
                mae="-0.3",
            ),
            _trade(
                symbol="ETHUSDT",
                side="SHORT",
                decision_index=34,
                entry_index=35,
                net_pnl="-6",
                exit_reason="HARD_STOP",
                mfe="0.6",
                mae="-1.1",
            ),
        ],
        "decision_events": [
            _entry_event(
                symbol="BTCUSDT",
                side="LONG",
                decision_index=34,
                entry_index=35,
                expected_edge="34",
                exit_mode="OPEN_ENDED_RUNNER",
            ),
            _entry_event(
                symbol="ETHUSDT",
                side="SHORT",
                decision_index=34,
                entry_index=35,
                expected_edge="28",
                exit_mode="FIXED_20_TARGET",
            ),
        ],
        "strategy_promotion_allowed": False,
        "bybit_live_order_routing_allowed": False,
    }


def test_diagnostics_reconstruct_completed_bar_features_and_group_outcomes() -> None:
    report = diagnose_crypto_historical_conditions(
        _acquisition(),
        _replay(),
        policy=CryptoHistoricalDiagnosticsPolicy(
            minimum_pattern_trades=1,
            quantile_buckets=2,
        ),
    )

    assert report["trade_count"] == 2
    assert report["aggregate"]["win_count"] == 1
    assert report["aggregate"]["loss_count"] == 1
    assert report["aggregate"]["total_net_pnl_usdt"] == 6.0
    assert set(report["by_symbol"]) == {"BTCUSDT", "ETHUSDT"}
    assert set(report["by_side"]) == {"LONG", "SHORT"}
    assert set(report["by_exit_mode"]) == {"FIXED_20_TARGET", "OPEN_ENDED_RUNNER"}
    assert report["coverage_by_symbol"]["BTCUSDT"]["bar_count"] == 36
    assert report["feature_quantiles"]["quality_score"]
    assert report["feature_quantiles"]["momentum_to_atr"]
    assert report["feature_quantiles"]["expected_net_edge_usd"]
    assert len(report["repeated_patterns"]) == 2
    assert all(item["sample_sufficient"] for item in report["repeated_patterns"])
    assert report["parameter_retuning_performed"] is False
    assert report["strategy_selection_allowed"] is False
    assert report["strategy_promotion_allowed"] is False
    assert report["demo_activation_allowed"] is False
    assert report["live_activation_allowed"] is False
    assert report["bybit_live_order_routing_allowed"] is False
    assert report["causal_claim_allowed"] is False
    assert report["predictive_guarantee_allowed"] is False


def test_repeated_patterns_are_marked_insufficient_below_minimum_sample() -> None:
    report = diagnose_crypto_historical_conditions(
        _acquisition(),
        _replay(),
        policy=CryptoHistoricalDiagnosticsPolicy(
            minimum_pattern_trades=5,
            quantile_buckets=2,
        ),
    )
    assert all(not item["sample_sufficient"] for item in report["repeated_patterns"])


def test_diagnostics_rejects_any_replay_without_explicit_no_live_boundary() -> None:
    replay = _replay()
    replay["bybit_live_order_routing_allowed"] = True
    with pytest.raises(ValueError, match="bybit_live_order_routing_allowed=false"):
        diagnose_crypto_historical_conditions(_acquisition(), replay)


def test_diagnostics_fails_closed_when_trade_has_no_matching_entry_event() -> None:
    replay = _replay()
    replay["decision_events"] = []
    with pytest.raises(ValueError, match="match closed trade to ENTRY"):
        diagnose_crypto_historical_conditions(_acquisition(), replay)
