from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.execution.bybit_demo import BybitDemoPosition
from app.execution.bybit_demo_cycle import BybitDemoCyclePolicy
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
from app.marketdata.bybit_demo_quotes import BybitDemoMarketQuote
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.marketdata.bybit_v5 import BybitKlineBar
from app.strategy.crypto_correlation import CryptoCorrelationPolicy
from app.strategy.crypto_perp import CryptoPerpStrategyConfig, CryptoTradePlan
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


def _two_long_histories() -> dict[str, tuple[BybitKlineBar, ...]]:
    return {
        "BTCUSDT": _bars("BTCUSDT", (100, 101, 102, 103, 104, 105, 106, 110)),
        "ETHUSDT": _bars("ETHUSDT", (100, 101, 102, 103, 104, 105, 106, 107)),
    }


def _correlated_long_histories() -> dict[str, tuple[BybitKlineBar, ...]]:
    closes = (100, 101, 102, 103, 104, 105, 106, 110)
    return {
        "BTCUSDT": _bars("BTCUSDT", closes),
        "ETHUSDT": _bars("ETHUSDT", closes),
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


def _instruments(
    *,
    histories: dict[str, tuple[BybitKlineBar, ...]] | None = None,
) -> dict[str, BybitInstrumentSpec]:
    active = _histories() if histories is None else histories
    return {symbol: _instrument(symbol) for symbol in active}


def _session(*, consecutive_losses: int = 0) -> CryptoSessionRiskState:
    return CryptoSessionRiskState(
        opening_equity_usdt=Decimal("1000"),
        current_equity_usdt=Decimal("1000"),
        peak_equity_usdt=Decimal("1000"),
        consecutive_losses=consecutive_losses,
    )


def _now() -> datetime:
    return datetime(2026, 8, 18, 0, 40, tzinfo=UTC)


def _quote(
    *,
    symbol: str = "BTCUSDT",
    ask: str = "108.1",
    bid: str = "108.0",
) -> BybitDemoMarketQuote:
    quote = BybitDemoMarketQuote(
        symbol=symbol,
        last_price=(Decimal(ask) + Decimal(bid)) / Decimal("2"),
        mark_price=(Decimal(ask) + Decimal(bid)) / Decimal("2"),
        bid_price=Decimal(bid),
        ask_price=Decimal(ask),
    )
    quote.validate()
    return quote


def _position(symbol: str) -> BybitDemoPosition:
    return BybitDemoPosition(
        symbol=symbol,
        side="Buy",
        size=Decimal("1"),
        average_price=Decimal("100"),
        unrealised_pnl=Decimal("0"),
        liquidation_price=Decimal("50"),
    )


class _QuoteClient:
    live_mainnet_order_routing_allowed = False

    def __init__(self, quote: BybitDemoMarketQuote) -> None:
        self.quote = quote
        self.calls: list[str] = []

    def get_quote(self, *, symbol: str) -> BybitDemoMarketQuote:
        self.calls.append(symbol)
        return self.quote


class _DemoClient:
    live_mainnet_order_routing_allowed = False

    def __init__(self, *positions: BybitDemoPosition) -> None:
        self.positions = positions
        self.position_reads = 0

    def get_positions(self) -> tuple[BybitDemoPosition, ...]:
        self.position_reads += 1
        return self.positions


def _orchestrator(observed: dict[str, object]):
    def fake(plan: object, **kwargs: object) -> BybitDemoOrchestratorResult:
        observed["plan"] = plan
        observed["instrument"] = kwargs["instrument"]
        return BybitDemoOrchestratorResult(
            status=BybitDemoOrchestratorStatus.CYCLE_EXECUTED,
            reasons=("TEST_GUARDED_PATH",),
            cycle_result=None,
            previous_trade_gate_checked=False,
            next_entry_allowed=False,
        )

    return fake


def _relaxed_correlation() -> CryptoCorrelationPolicy:
    return CryptoCorrelationPolicy(
        lookback_bars=3,
        minimum_return_observations=2,
        maximum_pairwise_correlation=Decimal("1"),
    )


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


def test_open_top_ranked_symbol_is_excluded_before_selecting_next_candidate() -> None:
    histories = _two_long_histories()
    baseline = select_bybit_demo_trade_plan(
        histories,
        instruments=_instruments(histories=histories),
        strategy_config=_config(),
        session_state=_session(),
        now=_now(),
    )
    assert baseline.selected_trade_plan is not None
    assert baseline.selected_trade_plan.symbol == "BTCUSDT"

    selection = select_bybit_demo_trade_plan(
        histories,
        instruments=_instruments(histories=histories),
        strategy_config=_config(),
        session_state=_session(),
        now=_now(),
        correlation_policy=_relaxed_correlation(),
        open_position_symbols=("BTCUSDT",),
        portfolio_state_checked=True,
    )

    assert selection.status is BybitDemoStrategySelectionStatus.SELECTED
    assert selection.selected_trade_plan is not None
    assert selection.selected_trade_plan.symbol == "ETHUSDT"
    assert selection.open_position_symbols == ("BTCUSDT",)
    btc = next(row for row in selection.candidate_audit if row.symbol == "BTCUSDT")
    assert btc.portfolio_reasons == ("PREEXISTING_SYMBOL_POSITION_EXCLUDED",)


def test_correlation_shadow_records_concentration_without_demo_activation() -> None:
    histories = _correlated_long_histories()
    selection = select_bybit_demo_trade_plan(
        histories,
        instruments=_instruments(histories=histories),
        strategy_config=_config(),
        session_state=_session(),
        now=_now(),
        correlation_policy=CryptoCorrelationPolicy(
            lookback_bars=3,
            minimum_return_observations=2,
            maximum_pairwise_correlation=Decimal("0.10"),
        ),
        open_position_symbols=("BTCUSDT",),
        portfolio_state_checked=True,
    )

    assert selection.status is BybitDemoStrategySelectionStatus.SELECTED
    assert selection.selected_trade_plan is not None
    assert selection.selected_trade_plan.symbol == "ETHUSDT"
    assert selection.correlation_block_count == 1
    eth = next(row for row in selection.candidate_audit if row.symbol == "ETHUSDT")
    assert eth.portfolio_reasons == ("PAIRWISE_CORRELATION_ABOVE_LIMIT",)
    assert eth.correlation_blocking_symbol == "BTCUSDT"
    assert eth.correlation == Decimal("1")
    assert selection.correlation_shadow_only is True
    assert selection.correlation_demo_activation_allowed is False


def test_portfolio_concurrency_blocks_selection_before_entry() -> None:
    histories = _two_long_histories()
    selection = select_bybit_demo_trade_plan(
        histories,
        instruments=_instruments(histories=histories),
        strategy_config=_config(),
        session_state=_session(),
        now=_now(),
        open_position_symbols=("BTCUSDT", "ETHUSDT"),
        portfolio_state_checked=True,
    )
    assert selection.status is BybitDemoStrategySelectionStatus.PORTFOLIO_CONCURRENCY_BLOCKED
    assert selection.selected_trade_plan is None
    assert selection.reasons == ("MAXIMUM_CONCURRENT_POSITIONS_REACHED",)


def test_selected_plan_is_passed_to_existing_guarded_orchestrator_without_bypass() -> None:
    observed: dict[str, object] = {}
    result = execute_selected_reconciled_guarded_bybit_demo_cycle(
        _histories(),
        instruments=_instruments(),
        strategy_config=_config(),
        session_state=_session(),
        now=_now(),
        client=object(),
        orchestrator=_orchestrator(observed),
    )

    assert result.status is BybitDemoStrategyCycleStatus.GUARDED_ORCHESTRATOR_CALLED
    assert result.selection.selected_trade_plan is observed["plan"]
    assert isinstance(observed["instrument"], BybitInstrumentSpec)
    assert result.orchestrator_result is not None
    assert result.orchestrator_result.reasons == ("TEST_GUARDED_PATH",)
    assert result.pre_entry_quote_checked is False
    assert result.live_mainnet_order_routing_allowed is False


def test_explicit_demo_write_path_rechecks_executable_quote_before_orchestrator() -> None:
    observed: dict[str, object] = {}
    quote_client = _QuoteClient(_quote())
    base_selection = select_bybit_demo_trade_plan(
        _histories(),
        instruments=_instruments(),
        strategy_config=_config(),
        session_state=_session(),
        now=_now(),
    )
    assert base_selection.selected_trade_plan is not None
    original_quantity = base_selection.selected_trade_plan.reference_quantity
    client = _DemoClient()

    result = execute_selected_reconciled_guarded_bybit_demo_cycle(
        _histories(),
        instruments=_instruments(),
        strategy_config=_config(),
        session_state=_session(),
        now=_now(),
        client=client,
        cycle_policy=BybitDemoCyclePolicy(writes_enabled=True),
        quote_client=quote_client,
        orchestrator=_orchestrator(observed),
    )

    assert result.status is BybitDemoStrategyCycleStatus.GUARDED_ORCHESTRATOR_CALLED
    assert client.position_reads == 1
    assert quote_client.calls == ["BTCUSDT"]
    assert result.selection.portfolio_state_checked is True
    assert result.pre_entry_quote_checked is True
    assert result.pre_entry_quote_price == Decimal("108.1")
    assert result.pre_entry_modeled_entry_price is not None
    assert result.pre_entry_adjusted_quantity is not None
    assert result.pre_entry_adjusted_quantity <= original_quantity
    assert result.selection.selected_trade_plan is observed["plan"]
    assert result.pre_entry_quote_reasons == ()


def test_explicit_write_path_selects_next_symbol_when_top_symbol_is_open() -> None:
    histories = _two_long_histories()
    client = _DemoClient(_position("BTCUSDT"))
    quote_client = _QuoteClient(_quote(symbol="ETHUSDT", ask="107.1", bid="107.0"))
    observed: dict[str, object] = {}

    result = execute_selected_reconciled_guarded_bybit_demo_cycle(
        histories,
        instruments=_instruments(histories=histories),
        strategy_config=_config(),
        session_state=_session(),
        now=_now(),
        client=client,
        cycle_policy=BybitDemoCyclePolicy(writes_enabled=True),
        correlation_policy=_relaxed_correlation(),
        quote_client=quote_client,
        orchestrator=_orchestrator(observed),
    )

    assert result.status is BybitDemoStrategyCycleStatus.GUARDED_ORCHESTRATOR_CALLED
    assert result.selection.selected_trade_plan is not None
    assert result.selection.selected_trade_plan.symbol == "ETHUSDT"
    assert result.selection.open_position_symbols == ("BTCUSDT",)
    assert quote_client.calls == ["ETHUSDT"]
    observed_plan = observed["plan"]
    assert isinstance(observed_plan, CryptoTradePlan)
    assert observed_plan.symbol == "ETHUSDT"


def test_explicit_write_path_blocks_if_portfolio_state_cannot_be_read() -> None:
    class BrokenPortfolioClient:
        live_mainnet_order_routing_allowed = False

        def get_positions(self) -> tuple[BybitDemoPosition, ...]:
            raise TimeoutError("demo positions")

    called = False

    def should_not_run(*_: object, **__: object) -> BybitDemoOrchestratorResult:
        nonlocal called
        called = True
        raise AssertionError("orchestrator must not run without portfolio state")

    result = execute_selected_reconciled_guarded_bybit_demo_cycle(
        _histories(),
        instruments=_instruments(),
        strategy_config=_config(),
        session_state=_session(),
        now=_now(),
        client=BrokenPortfolioClient(),
        cycle_policy=BybitDemoCyclePolicy(writes_enabled=True),
        quote_client=_QuoteClient(_quote()),
        orchestrator=should_not_run,
    )

    assert called is False
    assert result.status is BybitDemoStrategyCycleStatus.PORTFOLIO_STATE_BLOCKED
    assert result.selection.status is BybitDemoStrategySelectionStatus.PORTFOLIO_STATE_BLOCKED
    assert result.selection.reasons == ("DEMO_PORTFOLIO_READ_FAILED:TimeoutError",)


def test_explicit_demo_write_path_blocks_when_quote_read_fails() -> None:
    class BrokenQuoteClient:
        live_mainnet_order_routing_allowed = False

        def get_quote(self, *, symbol: str) -> BybitDemoMarketQuote:
            raise TimeoutError(symbol)

    called = False

    def should_not_run(*_: object, **__: object) -> BybitDemoOrchestratorResult:
        nonlocal called
        called = True
        raise AssertionError("orchestrator must not run after quote-read failure")

    result = execute_selected_reconciled_guarded_bybit_demo_cycle(
        _histories(),
        instruments=_instruments(),
        strategy_config=_config(),
        session_state=_session(),
        now=_now(),
        client=_DemoClient(),
        cycle_policy=BybitDemoCyclePolicy(writes_enabled=True),
        quote_client=BrokenQuoteClient(),
        orchestrator=should_not_run,
    )

    assert called is False
    assert result.status is BybitDemoStrategyCycleStatus.PRE_ENTRY_QUOTE_BLOCKED
    assert result.pre_entry_quote_checked is True
    assert result.pre_entry_quote_reasons == ("PRE_ENTRY_QUOTE_READ_FAILED:TimeoutError",)


def test_explicit_demo_write_path_blocks_when_quote_destroys_minimum_edge() -> None:
    low_quote = _quote(ask="10", bid="9.9")
    called = False

    def should_not_run(*_: object, **__: object) -> BybitDemoOrchestratorResult:
        nonlocal called
        called = True
        raise AssertionError("orchestrator must not run after quote economics fail")

    result = execute_selected_reconciled_guarded_bybit_demo_cycle(
        _histories(),
        instruments=_instruments(),
        strategy_config=_config(),
        session_state=_session(),
        now=_now(),
        client=_DemoClient(),
        cycle_policy=BybitDemoCyclePolicy(writes_enabled=True),
        quote_client=_QuoteClient(low_quote),
        orchestrator=should_not_run,
    )

    assert called is False
    assert result.status is BybitDemoStrategyCycleStatus.PRE_ENTRY_QUOTE_BLOCKED
    assert result.pre_entry_quote_checked is True
    assert result.pre_entry_quote_price == Decimal("10")
    assert "NEXT_OPEN_EXPECTED_NET_PROFIT_BELOW_TARGET" in result.pre_entry_quote_reasons


def test_explicit_demo_write_path_rejects_mainnet_capable_quote_reader() -> None:
    class UnsafeQuoteClient:
        live_mainnet_order_routing_allowed = True

    with pytest.raises(ValueError, match="mainnet-capable quote reader"):
        execute_selected_reconciled_guarded_bybit_demo_cycle(
            _histories(),
            instruments=_instruments(),
            strategy_config=_config(),
            session_state=_session(),
            now=_now(),
            client=_DemoClient(),
            cycle_policy=BybitDemoCyclePolicy(writes_enabled=True),
            quote_client=UnsafeQuoteClient(),
            orchestrator=_orchestrator({}),
        )
