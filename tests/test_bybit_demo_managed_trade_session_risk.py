from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from app.execution.bybit_demo_excursion_runtime import BybitDemoExcursionRuntimeStatus
from app.execution.bybit_demo_managed_trade_poll import (
    BybitDemoManagedTradePollPhase,
    poll_bybit_demo_managed_trade,
)
from app.execution.bybit_demo_session_risk_flatten import (
    BybitDemoSessionRiskFlattenStatus,
)
from app.execution.bybit_demo_trade_management_runtime import (
    BybitDemoTradeManagementRuntimeStatus,
)
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.strategy.crypto_perp import CryptoPerpStrategyConfig
from app.strategy.crypto_session_risk import CryptoSessionRiskState


class _Safe:
    live_mainnet_order_routing_allowed = False


def _instrument() -> BybitInstrumentSpec:
    return BybitInstrumentSpec(
        symbol="BTCUSDT",
        status="Trading",
        contract_type="LinearPerpetual",
        base_coin="BTC",
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


def _state(*, current: str, peak: str = "1000") -> CryptoSessionRiskState:
    return CryptoSessionRiskState(
        opening_equity_usdt=Decimal("1000"),
        current_equity_usdt=Decimal(current),
        peak_equity_usdt=Decimal(peak),
    )


def _open_excursion():
    return SimpleNamespace(
        status=BybitDemoExcursionRuntimeStatus.OPEN_OBSERVED,
        reasons=(),
        live_mainnet_order_routing_allowed=False,
    )


def test_flatten_required_has_priority_over_normal_trade_management() -> None:
    management_calls = 0
    flatten_calls = 0

    def _management(**_kwargs):
        nonlocal management_calls
        management_calls += 1
        return SimpleNamespace(
            status=BybitDemoTradeManagementRuntimeStatus.NO_CHANGE,
            reasons=(),
            live_mainnet_order_routing_allowed=False,
        )

    def _flatten(**kwargs):
        nonlocal flatten_calls
        flatten_calls += 1
        assert kwargs["session_state"].current_equity_usdt == Decimal("940")
        return SimpleNamespace(
            status=BybitDemoSessionRiskFlattenStatus.WRITES_DISABLED,
            reasons=("SESSION_RISK_FLATTEN_WRITES_DISABLED",),
            live_mainnet_order_routing_allowed=False,
        )

    result = poll_bybit_demo_managed_trade(
        excursion_store=_Safe(),
        trade_client=_Safe(),
        completed_bar_client=_Safe(),
        quote_client=_Safe(),
        instrument=_instrument(),
        strategy_config=CryptoPerpStrategyConfig(),
        now_ms=1_000,
        session_state=_state(current="940"),
        advance_excursion=lambda **_kwargs: _open_excursion(),
        run_management=_management,
        run_session_risk_flatten=_flatten,
    )

    assert result.phase is BybitDemoManagedTradePollPhase.SESSION_RISK_ACTION
    assert result.session_risk_flatten is not None
    assert flatten_calls == 1
    assert management_calls == 0
    assert "SESSION_DRAWDOWN_LIMIT_BREACHED" in result.reasons
    assert result.next_entry_allowed is False


def test_nonflattening_session_continues_normal_management() -> None:
    management_calls = 0
    flatten_calls = 0

    def _management(**_kwargs):
        nonlocal management_calls
        management_calls += 1
        return SimpleNamespace(
            status=BybitDemoTradeManagementRuntimeStatus.NO_CHANGE,
            reasons=("NO_CHANGE",),
            live_mainnet_order_routing_allowed=False,
        )

    def _flatten(**_kwargs):
        nonlocal flatten_calls
        flatten_calls += 1
        raise AssertionError("flatten must not run")

    result = poll_bybit_demo_managed_trade(
        excursion_store=_Safe(),
        trade_client=_Safe(),
        completed_bar_client=_Safe(),
        quote_client=_Safe(),
        instrument=_instrument(),
        strategy_config=CryptoPerpStrategyConfig(),
        now_ms=1_000,
        session_state=_state(current="990"),
        advance_excursion=lambda **_kwargs: _open_excursion(),
        run_management=_management,
        run_session_risk_flatten=_flatten,
    )

    assert result.phase is BybitDemoManagedTradePollPhase.OPEN_MANAGED
    assert result.session_risk_flatten is None
    assert result.reasons == ("NO_CHANGE",)
    assert management_calls == 1
    assert flatten_calls == 0
