from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.strategy.crypto_derivatives_context import CryptoTradeDerivativesContext
from app.strategy.crypto_historical_diagnostics import CryptoHistoricalTradeCondition
from app.strategy.crypto_strategy_evidence_matrix import (
    CryptoStrategyEvidencePolicy,
    CryptoTradeExecutionEconomics,
    build_crypto_strategy_evidence_rows,
    build_crypto_trade_execution_economics,
    diagnose_crypto_strategy_evidence_matrix,
)

_START = datetime(2026, 1, 1, tzinfo=UTC)


def _time(hours: int) -> str:
    return (_START + timedelta(hours=hours)).isoformat()


def _condition(index: int, pnl: str) -> CryptoHistoricalTradeCondition:
    return CryptoHistoricalTradeCondition(
        symbol="BTCUSDT",
        side="LONG",
        decision_time=_time(index * 3),
        entry_time=_time(index * 3 + 1),
        exit_time=_time(index * 3 + 2),
        exit_reason="TARGET",
        net_pnl_usdt=Decimal(pnl),
        maximum_favorable_r=Decimal("1.5"),
        maximum_adverse_r=Decimal("-0.4"),
        holding_bars=4,
        quality_score=Decimal("1.4"),
        momentum_abs=Decimal("0.01"),
        momentum_to_atr=Decimal("1.2"),
        atr_fraction=Decimal("0.020"),
        trend_strength_atr=Decimal("1.3"),
        breakout_strength_atr=Decimal("0.2"),
        one_bar_atr_multiple=Decimal("1.4"),
        average_turnover_usdt=Decimal("2000000"),
        expected_net_edge_usd=Decimal("30"),
        exit_mode="FIXED_20_TARGET",
        volatility_regime="VOL_HIGH_NORMAL",
        trend_regime="TREND_STRONG",
        breakout_regime="BREAKOUT_CONFIRMED",
        turnover_regime="TURNOVER_HIGH",
    )


def _derivatives(index: int, *, complete: bool = True) -> CryptoTradeDerivativesContext:
    missing = () if complete else ("OPEN_INTEREST_PREVIOUS_POINT_MISSING",)
    return CryptoTradeDerivativesContext(
        symbol="BTCUSDT",
        side="LONG",
        decision_time=_time(index * 3),
        entry_time=_time(index * 3 + 1),
        exit_time=_time(index * 3 + 2),
        exit_reason="TARGET",
        net_pnl_usdt=Decimal("0"),
        maximum_favorable_r=Decimal("0"),
        maximum_adverse_r=Decimal("0"),
        open_interest_timestamp_ms=1,
        open_interest=Decimal("102"),
        previous_open_interest=Decimal("100") if complete else None,
        open_interest_delta=Decimal("2") if complete else None,
        open_interest_delta_fraction=Decimal("0.02") if complete else None,
        account_ratio_timestamp_ms=1,
        long_account_ratio=Decimal("0.60"),
        short_account_ratio=Decimal("0.40"),
        long_short_account_ratio=Decimal("1.5"),
        prior_funding_timestamp_ms=1,
        prior_funding_rate=Decimal("0.0001"),
        holding_funding_rate_sum=Decimal("0"),
        holding_funding_event_count=0,
        decision_context_complete=complete,
        missing_reasons=missing,
    )


def _economics(index: int) -> CryptoTradeExecutionEconomics:
    entry_price = Decimal("100")
    quantity = Decimal("2")
    expected_edge = Decimal("30")
    risk = Decimal("10")
    cost = Decimal("0.32")
    return CryptoTradeExecutionEconomics(
        symbol="BTCUSDT",
        side="LONG",
        decision_time=_time(index * 3),
        entry_time=_time(index * 3 + 1),
        entry_price=entry_price,
        quantity=quantity,
        notional_usdt=entry_price * quantity,
        expected_net_edge_usd=expected_edge,
        minimum_entry_net_edge_usd=Decimal("20"),
        risk_budget_usdt=risk,
        modeled_round_trip_cost_usdt=cost,
        cost_to_expected_edge=cost / expected_edge,
        expected_edge_to_risk=expected_edge / risk,
    )


def test_execution_economics_reconstructs_fixed_fee_and_slippage_cost() -> None:
    replay = {
        "decision_events": [
            {
                "event": "ENTRY",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "decision_time": _time(0),
                "execution_time": _time(1),
                "entry_price": 100,
                "quantity": 2,
                "minimum_entry_net_edge_usd": 20,
                "expected_net_edge_usd": 30,
                "risk_budget_usdt": 10,
            }
        ],
        "strategy_promotion_allowed": False,
        "bybit_live_order_routing_allowed": False,
    }
    row = build_crypto_trade_execution_economics(replay)[0]
    assert row.notional_usdt == Decimal("200")
    assert row.modeled_round_trip_cost_usdt == Decimal("0.3200")
    assert row.cost_to_expected_edge == Decimal("0.3200") / Decimal("30")
    assert row.expected_edge_to_risk == Decimal("3")


def test_evidence_rows_combine_price_derivatives_stress_and_execution_without_liquidation_claim() -> None:
    rows = build_crypto_strategy_evidence_rows(
        (_condition(0, "10"),),
        (_derivatives(0),),
        (_economics(0),),
    )
    row = rows[0]
    assert row.market_regime == (
        "VOL_HIGH_NORMAL|TREND_STRONG|BREAKOUT_CONFIRMED|TURNOVER_HIGH"
    )
    assert row.open_interest_regime == "OI_RISING"
    assert row.crowding_regime == "LONG_HEAVY"
    assert row.prior_funding_regime == "FUNDING_POSITIVE"
    assert row.stress_score == 5
    assert row.stress_regime == "STRESS_HIGH"
    assert row.stress_feature_complete is True
    assert "OPEN_INTEREST_IMPULSE" in row.stress_reasons
    assert "CROWDED_SIDE_PAYS_PRIOR_FUNDING" in row.stress_reasons
    assert row.liquidation_history_available is False
    assert row.liquidation_event_source == "NOT_RECONSTRUCTED"


def test_incomplete_derivatives_context_never_fakes_stress_state() -> None:
    row = build_crypto_strategy_evidence_rows(
        (_condition(0, "10"),),
        (_derivatives(0, complete=False),),
        (_economics(0),),
    )[0]
    assert row.stress_feature_complete is False
    assert row.stress_regime == "STRESS_UNKNOWN"
    assert "OPEN_INTEREST_PREVIOUS_POINT_MISSING" in row.stress_reasons


def test_matrix_reports_requested_profit_factor_win_rate_mfe_mae_drawdown_and_sample() -> None:
    pnls = ("10", "-5", "8", "-12", "4", "6")
    conditions = tuple(_condition(index, pnl) for index, pnl in enumerate(pnls))
    derivatives = tuple(_derivatives(index) for index in range(len(pnls)))
    economics = tuple(_economics(index) for index in range(len(pnls)))
    rows = build_crypto_strategy_evidence_rows(conditions, derivatives, economics)
    report = diagnose_crypto_strategy_evidence_matrix(
        rows,
        policy=CryptoStrategyEvidencePolicy(minimum_cell_trades=5),
    )

    assert report["trade_count"] == 6
    assert report["cell_count"] == 1
    cell = report["matrix"][0]
    assert cell["symbol"] == "BTCUSDT"
    assert cell["side"] == "LONG"
    assert cell["trade_count"] == 6
    assert cell["sample_sufficient"] is True
    assert cell["win_count"] == 4
    assert cell["loss_count"] == 2
    assert cell["win_rate"] == pytest.approx(4 / 6)
    assert cell["total_net_pnl_usdt"] == 11.0
    assert cell["profit_factor"] == pytest.approx(28 / 17)
    assert cell["average_mfe_r"] == 1.5
    assert cell["average_mae_r"] == -0.4
    assert cell["maximum_trade_sequence_drawdown_usdt"] == 12.0
    assert cell["average_modeled_round_trip_cost_usdt"] == 0.32
    assert report["liquidation_context"] == {
        "historical_market_wide_liquidation_events_available": False,
        "source": "NOT_RECONSTRUCTED",
        "stress_proxy_used_instead": True,
    }
    assert report["parameter_retuning_performed"] is False
    assert report["strategy_selection_allowed"] is False
    assert report["strategy_promotion_allowed"] is False
    assert report["demo_activation_allowed"] is False
    assert report["live_activation_allowed"] is False
    assert report["bybit_live_order_routing_allowed"] is False
    assert report["causal_claim_allowed"] is False
    assert report["predictive_guarantee_allowed"] is False


def test_evidence_identity_and_live_capable_replay_fail_closed() -> None:
    bad_economics = _economics(0)
    bad_economics = CryptoTradeExecutionEconomics(
        **{**bad_economics.__dict__, "decision_time": _time(99)}
    )
    with pytest.raises(ValueError, match="execution economics identity mismatch"):
        build_crypto_strategy_evidence_rows(
            (_condition(0, "10"),),
            (_derivatives(0),),
            (bad_economics,),
        )

    replay = {
        "decision_events": [],
        "strategy_promotion_allowed": False,
        "bybit_live_order_routing_allowed": True,
    }
    with pytest.raises(ValueError, match="bybit_live_order_routing_allowed=false"):
        build_crypto_trade_execution_economics(replay)
