from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.marketdata.bybit_v5 import BybitKlineAcquisition, BybitKlineBar
from app.strategy.crypto_perp import CryptoPerpStrategyConfig
from app.strategy.crypto_trade_management import CryptoProtectionPolicy
from tools.research_bybit_crypto_strategy_v2 import (
    compact_candidate_comparison,
    run_crypto_strategy_v2_suite,
)


def _bar(symbol: str, index: int, *, direction: int, start: datetime) -> BybitKlineBar:
    base = Decimal("100") if direction > 0 else Decimal("140")
    close = base + Decimal(direction) * Decimal("0.7") * Decimal(index)
    return BybitKlineBar(
        symbol=symbol,
        start_time=start + timedelta(minutes=5 * index),
        open=close - Decimal(direction) * Decimal("0.10"),
        high=close + Decimal("0.50"),
        low=close - Decimal("0.50"),
        close=close,
        volume=Decimal("10000"),
        turnover=Decimal("2000000"),
    )


def _acquisition(count: int = 120) -> BybitKlineAcquisition:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    bars = []
    for symbol, direction in (("BTCUSDT", 1), ("ETHUSDT", 1), ("SOLUSDT", -1)):
        bars.extend(_bar(symbol, index, direction=direction, start=start) for index in range(count))
    return BybitKlineAcquisition(
        bars=tuple(sorted(bars, key=lambda item: (item.symbol, item.start_time))),
        pages_by_symbol={"BTCUSDT": 1, "ETHUSDT": 1, "SOLUSDT": 1},
    )


def _config() -> CryptoPerpStrategyConfig:
    return CryptoPerpStrategyConfig(
        minimum_average_turnover_usdt=Decimal("1000"),
        minimum_atr_fraction=Decimal("0.0001"),
        maximum_atr_fraction=Decimal("0.10"),
        minimum_abs_momentum=Decimal("0.001"),
        minimum_quality_score=Decimal("0.10"),
        maximum_one_bar_atr_multiple=Decimal("5"),
        risk_fraction_per_trade=Decimal("0.01"),
        maximum_notional_to_equity=Decimal("2"),
        expected_move_atr_multiple=Decimal("10"),
        taker_fee_rate=Decimal("0.0006"),
        slippage_bps_per_fill=Decimal("2"),
        maximum_concurrent_positions=2,
    )


def test_strategy_v2_suite_keeps_all_candidates_shadow_only() -> None:
    suite = run_crypto_strategy_v2_suite(
        _acquisition(),
        opening_equity_usdt=Decimal("1000"),
        base_config=_config(),
        protection_policy=CryptoProtectionPolicy(maximum_holding_bars=12),
    )

    assert suite["suite"] == "BYBIT_CRYPTO_STRATEGY_V2_SHADOW"
    assert suite["candidate_contract"]["minimum_entry_net_profit_usd"] == 20.0
    assert suite["candidate_contract"]["runner_minimum_expected_edge_multiple"] == 1.5
    tight = suite["candidate_contract"]["tight_profit_lock_hypothesis"]
    assert tight["break_even_activation_r"] == 0.8
    assert tight["profit_lock_activation_r"] == 1.0
    assert tight["profit_lock_r"] == 0.5
    assert tight["same_sample_14d_promotion_allowed"] is False
    assert tight["requires_walk_forward_validation"] is True
    assert suite["strategy_promotion_allowed"] is False
    assert suite["demo_order_writes_enabled"] is False
    assert suite["live_promotion_allowed"] is False
    assert suite["bybit_live_order_routing_allowed"] is False
    assert set(suite["candidates"]) == {
        "CONDITIONAL_1_5X",
        "CONDITIONAL_TIGHT_PROFIT_LOCK",
        "CONDITIONAL_SESSION_RISK",
        "CONDITIONAL_DIVERSIFIED",
        "CONDITIONAL_EXECUTION_RISK",
        "CONDITIONAL_COMBINED_RISK",
    }
    for candidate in suite["candidates"].values():
        assert candidate["strategy_promotion_allowed"] is False
        assert candidate["bybit_demo_order_writes_enabled"] is False
        assert candidate["bybit_live_order_routing_allowed"] is False


def test_strategy_v2_suite_exposes_only_overlay_differences_in_comparison() -> None:
    suite = run_crypto_strategy_v2_suite(
        _acquisition(),
        opening_equity_usdt=Decimal("1000"),
        base_config=_config(),
        protection_policy=CryptoProtectionPolicy(maximum_holding_bars=12),
    )
    comparison = compact_candidate_comparison(suite)

    assert set(comparison) == set(suite["candidates"])
    assert comparison["CONDITIONAL_1_5X"]["session_risk"]["enabled"] is False
    assert comparison["CONDITIONAL_TIGHT_PROFIT_LOCK"]["session_risk"]["enabled"] is False
    assert comparison["CONDITIONAL_SESSION_RISK"]["session_risk"]["enabled"] is True
    assert comparison["CONDITIONAL_DIVERSIFIED"]["correlation_diversification"][
        "enabled"
    ] is True
    assert comparison["CONDITIONAL_EXECUTION_RISK"]["execution_risk"]["enabled"] is True
    combined = comparison["CONDITIONAL_COMBINED_RISK"]
    assert combined["session_risk"]["enabled"] is True
    assert combined["correlation_diversification"]["enabled"] is True
    assert combined["execution_risk"]["enabled"] is True
