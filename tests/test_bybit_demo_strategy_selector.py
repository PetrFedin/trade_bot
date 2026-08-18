from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.execution.bybit_demo_orchestrator import (
    BybitDemoOrchestratorResult,
    BybitDemoOrchestratorStatus,
)
from app.execution.bybit_demo_strategy_selector import (
    BybitDemoStrategyCycleStatus,
    BybitDemoStrategySelectionStatus,
    execute_selected_reconciled_guarded_bybit_demo_cycle,
    select_bybit_demo_trade_plan,
)
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.marketdata.bybit_v5 import BybitKlineBar
from app.strategy.crypto_perp import CryptoPerpStrategyConfig
from app.strategy.crypto_session_risk import CryptoSessionRiskState


def _config() -> CryptoPerpStrategyConfig:
    return CryptoPerpStrategyConfig(
        fast_ema_bars=2,
        slow_ema_bars=3,
        momentum_bars=2,
        breakout_bars=2,
        atr_bars=2,
        turnover_bars=2,
        minimum_average_turnover_usdt=Decimal("1"),
        minimum_atr_fraction=Decimal("0.0001"),
        maximum_atr_fraction=Decimal("0.10"),
        minimum_abs_momentum=Decimal("0.001"),
        minimum_quality_score=Decimal("0"),
        maximum_one_bar_atr_multiple=Decimal("10"),
        expected_move_atr_multiple=Decimal("4"),
        target_net_profit_usd=Decimal("20"),
    )


def _bars(symbol: str, closes: tuple[int, ...]) -> tuple[BybitKlineBar, ...]:
    start = datetime(2026, 8, 18, tzinfo=UTC)
    return tuple(
        BybitKlineBar(
            symbol=symbol,
            start_time=start + timedelta(minutes=5 * index),
            open=Decimal(close) - Decimal("0.5"),
            high=Decimal(close) + Decimal("1"),
            low=Decimal(close) - Decimal("1"),
            close=Decimal(close),
            volume=Decimal("100"),
            turnover=Decimal("1000000"),
        )
        for index, close in enumerate(closes)
    )


def _histories() -> dict[str, tuple[BybitKlineBar, ...]]:
    return {
        "BTCUSDT": _bars("BTCUSDT", (100, 101, 102, 103, 104, 105, 106, 108)),
        "ETHUSDT": _bars("ETHUSDT", (100, 100, 100, 100, 100, 100, 100, 100)),
    }


def _instrument(symbol: str) -> BybitInstrumentSpec:
    return BybitInstrumentSpec(
        symbol=symbol,
        status="Trading",
        contract_type="LinearPerpetual",
        base_coin=symbol.removesuffix("USDT"),
        quote_coin="USDT",
        settle_coin="USDT",
        tick_size=Decimal("0.1"),
        min_order_qty=Decimal("0.001"),
        qty_step=Decimal("0.001"),
        min_notional_value=Decimal("5"),
        max_market_order_qty=Decimal("1000"),
        max_leverage=Decimal("100"),
        funding_interval_minutes=480,
    )


def _instruments() -> dict[str, BybitInstrumentSpec]:
    return {symbol: _instrument(symbol) for symbol in _histories()}


def _session(*, consecutive_losses: int = 0) -> CryptoSessionRiskState:
    return CryptoSessionRiskState(
        opening_equity_usdt=Decimal("1000"),
        current_equity_usdt=Decimal("1000"),
        peak_equity_usdt=Decimal("1000"),
        consecutive_losses=consecutive_losses,
    )


def _now() -> datetime:
    return datetime(2026, 8, 18, 0, 40, tzinfo=UTC)


def test_demo_strategy_selector_bridges_completed_bars_to_one_demo_ready_plan() -> None:
    selection = select_bybit_demo_trade_plan(
        _histories(),
        instruments=_instruments(),
        strategy_config=_config(),
        session_state=_session(),
        now=_now(),
    )

    assert selection.status is BybitDemoStrategySelectionStatus.SELECTED
    assert selection.selected_trade_plan is not None
    assert selection.selected_trade_plan.symbol == "BTCUSDT"
    assert selection.selected_entry_preflight is not None
    assert selection.selected_entry_preflight.eligible is True
    assert selection.executable_candidate_count == 1
    assert selection.economic_shadow_selected_symbol == "BTCUSDT"
    assert selection.economic_shadow_differs_from_current is False
    assert selection.economic_shadow_activation_allowed is False
    assert selection.order_write_performed is False
    assert selection.live_mainnet_order_routing_allowed is False


def test_demo_strategy_selector_fails_closed_on_session_loss_streak() -> None:
    selection = select_bybit_demo_trade_plan(
        _histories(),
        instruments=_instruments(),
        strategy_config=_config(),
        session_state=_session(consecutive_losses=3),
        now=_now(),
    )

    assert selection.status is BybitDemoStrategySelectionStatus.SESSION_RISK_BLOCKED
    assert selection.selected_trade_plan is None
    assert "SESSION_CONSECUTIVE_LOSS_LIMIT_REACHED" in selection.reasons
    assert selection.order_write_performed is False


def test_demo_strategy_selector_rejects_incomplete_latest_bar() -> None:
    with pytest.raises(ValueError, match="incomplete latest bar"):
        select_bybit_demo_trade_plan(
            _histories(),
            instruments=_instruments(),
            strategy_config=_config(),
            session_state=_session(),
            now=datetime(2026, 8, 18, 0, 39, 59, tzinfo=UTC),
        )


def test_demo_strategy_selector_requires_instrument_preflight() -> None:
    selection = select_bybit_demo_trade_plan(
        _histories(),
        instruments={"ETHUSDT": _instrument("ETHUSDT")},
        strategy_config=_config(),
        session_state=_session(),
        now=_now(),
    )
    assert selection.status is BybitDemoStrategySelectionStatus.NO_EXECUTABLE_PLAN
    assert selection.selected_trade_plan is None
    btc = next(row for row in selection.candidate_audit if row.symbol == "BTCUSDT")
    assert btc.demo_preflight_reasons == ("BYBIT_INSTRUMENT_SPEC_UNAVAILABLE",)


def test_selected_plan_is_passed_to_existing_guarded_orchestrator_without_bypass() -> None:
    observed: dict[str, object] = {}

    def fake_orchestrator(plan: object, **kwargs: object) -> BybitDemoOrchestratorResult:
        observed["plan"] = plan
        observed["instrument"] = kwargs["instrument"]
        return BybitDemoOrchestratorResult(
            status=BybitDemoOrchestratorStatus.CYCLE_EXECUTED,
            reasons=("TEST_GUARDED_PATH",),
            cycle_result=None,
            previous_trade_gate_checked=False,
            next_entry_allowed=False,
        )

    result = execute_selected_reconciled_guarded_bybit_demo_cycle(
        _histories(),
        instruments=_instruments(),
        strategy_config=_config(),
        session_state=_session(),
        now=_now(),
        client=object(),
        orchestrator=fake_orchestrator,
    )

    assert result.status is BybitDemoStrategyCycleStatus.GUARDED_ORCHESTRATOR_CALLED
    assert result.selection.selected_trade_plan is observed["plan"]
    assert isinstance(observed["instrument"], BybitInstrumentSpec)
    assert result.orchestrator_result is not None
    assert result.orchestrator_result.reasons == ("TEST_GUARDED_PATH",)
    assert result.live_mainnet_order_routing_allowed is False
