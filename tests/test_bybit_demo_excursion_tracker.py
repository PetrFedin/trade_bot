from __future__ import annotations

from decimal import Decimal

from app.execution.bybit_demo import BybitDemoPosition
from app.execution.bybit_demo_excursion_tracker import (
    finalize_bybit_demo_trade_excursion,
    observe_bybit_demo_trade_excursion,
    start_bybit_demo_trade_excursion,
    summarize_bybit_demo_excursion_quality,
)
from app.execution.bybit_demo_trade_monitor import (
    BybitDemoTradeMonitorResult,
    BybitDemoTradeMonitorStatus,
)
from app.marketdata.bybit_demo_quotes import BybitDemoMarketQuote
from app.strategy.crypto_perp import CryptoSide, CryptoTradePlan


def _plan(side: CryptoSide = CryptoSide.LONG) -> CryptoTradePlan:
    return CryptoTradePlan(
        symbol="BTCUSDT",
        side=side,
        decision_time="2026-08-18T20:00:00+00:00",
        reference_price=Decimal("100"),
        notional_usdt=Decimal("200"),
        reference_quantity=Decimal("2"),
        risk_budget_usdt=Decimal("10"),
        stop_fraction=Decimal("0.05"),
        estimated_round_trip_cost_usdt=Decimal("1"),
        estimated_stop_loss_after_cost_usdt=Decimal("11"),
        target_net_profit_usd=Decimal("20"),
        required_move_fraction=Decimal("0.105"),
        expected_move_fraction=Decimal("0.15"),
        expected_net_edge_usd=Decimal("29"),
        quality_score=Decimal("2"),
    )


def _position(
    *,
    side: str = "Buy",
    size: str = "2",
    entry: str = "100",
    unrealised: str = "0",
) -> BybitDemoPosition:
    return BybitDemoPosition(
        symbol="BTCUSDT",
        side=side,
        size=Decimal(size),
        average_price=Decimal(entry),
        unrealised_pnl=Decimal(unrealised),
        liquidation_price=Decimal("50") if side == "Buy" else Decimal("150"),
    )


def _quote(mark: str, *, server_time_ms: int) -> BybitDemoMarketQuote:
    mark_price = Decimal(mark)
    quote = BybitDemoMarketQuote(
        symbol="BTCUSDT",
        last_price=mark_price,
        mark_price=mark_price,
        bid_price=mark_price - Decimal("0.01"),
        ask_price=mark_price + Decimal("0.01"),
        server_time_ms=server_time_ms,
        received_time_ms=server_time_ms + 100,
        age_ms=100,
    )
    quote.validate()
    return quote


def _terminal_trade(
    *,
    side: str = "Buy",
    exit_price: str,
    entry_price: str = "100",
) -> BybitDemoTradeMonitorResult:
    entry = Decimal(entry_price)
    exit_value = Decimal(exit_price)
    gross = (
        (exit_value - entry) * Decimal("2")
        if side == "Buy"
        else (entry - exit_value) * Decimal("2")
    )
    return BybitDemoTradeMonitorResult(
        status=BybitDemoTradeMonitorStatus.CLOSED_RECONCILED,
        symbol="BTCUSDT",
        entry_order_link_id="ASTRA-DEMO-E-EXCURSION",
        entry_side=side,
        entry_quantity=Decimal("2"),
        exit_quantity=Decimal("2"),
        remaining_quantity=Decimal("0"),
        average_entry_price=entry,
        average_exit_price=exit_value,
        entry_fees_usdt=Decimal("0.1"),
        exit_fees_usdt=Decimal("0.1"),
        execution_fees_usdt=Decimal("0.2"),
        realized_gross_pnl_usdt=gross,
        realized_net_pnl_after_execution_fees_usdt=gross - Decimal("0.2"),
        reasons=(),
        terminal=True,
        next_entry_allowed=True,
    )


def test_long_excursion_tracks_mfe_mae_and_giveback_in_r() -> None:
    state = start_bybit_demo_trade_excursion(_plan(), position=_position())
    state = observe_bybit_demo_trade_excursion(
        state,
        position=_position(unrealised="10"),
        quote=_quote("105", server_time_ms=1_000),
    )
    state = observe_bybit_demo_trade_excursion(
        state,
        position=_position(unrealised="-10"),
        quote=_quote("95", server_time_ms=2_000),
    )

    assert state.observation_count == 2
    assert state.observed_peak_favorable_r == Decimal("1")
    assert state.observed_trough_r == Decimal("-1")
    assert state.latest_gross_r == Decimal("-1")
    assert state.latest_giveback_from_peak_r == Decimal("2")


def test_short_excursion_uses_linear_contract_return_not_inverse_price_return() -> None:
    state = start_bybit_demo_trade_excursion(
        _plan(CryptoSide.SHORT),
        position=_position(side="Sell"),
    )
    state = observe_bybit_demo_trade_excursion(
        state,
        position=_position(side="Sell", unrealised="10"),
        quote=_quote("95", server_time_ms=1_000),
    )

    assert state.latest_gross_r == Decimal("1")
    assert state.observed_peak_favorable_r == Decimal("1")
    assert state.projected_initial_quantity_gross_pnl_usdt == Decimal("10")


def test_partial_close_keeps_initial_excursion_basis_and_tracks_current_exposure() -> None:
    state = start_bybit_demo_trade_excursion(_plan(), position=_position())
    state = observe_bybit_demo_trade_excursion(
        state,
        position=_position(size="1", unrealised="10"),
        quote=_quote("110", server_time_ms=1_000),
    )

    assert state.partial_close_seen is True
    assert state.current_quantity == Decimal("1")
    assert state.observed_peak_favorable_r == Decimal("2")
    assert state.projected_initial_quantity_gross_pnl_usdt == Decimal("20")
    assert state.current_quantity_gross_pnl_usdt == Decimal("10")


def test_terminal_exit_capture_and_giveback_are_measured_against_observed_peak() -> None:
    state = start_bybit_demo_trade_excursion(_plan(), position=_position())
    state = observe_bybit_demo_trade_excursion(
        state,
        position=_position(unrealised="20"),
        quote=_quote("110", server_time_ms=1_000),
    )

    final = finalize_bybit_demo_trade_excursion(
        state,
        trade=_terminal_trade(exit_price="102"),
    )

    assert final.observed_peak_favorable_r == Decimal("2")
    assert final.realized_gross_exit_r == Decimal("0.4")
    assert final.observed_peak_capture_fraction == Decimal("0.2")
    assert final.giveback_from_observed_peak_to_exit_r == Decimal("1.6")
    assert final.positive_observed_peak_nonpositive_exit is False


def test_positive_observed_peak_that_closes_nonpositive_is_flagged() -> None:
    state = start_bybit_demo_trade_excursion(_plan(), position=_position())
    state = observe_bybit_demo_trade_excursion(
        state,
        position=_position(unrealised="10"),
        quote=_quote("105", server_time_ms=1_000),
    )

    final = finalize_bybit_demo_trade_excursion(
        state,
        trade=_terminal_trade(exit_price="99"),
    )

    assert final.realized_gross_exit_r == Decimal("-0.2")
    assert final.positive_observed_peak_nonpositive_exit is True
    assert final.giveback_from_observed_peak_to_exit_r == Decimal("1.2")


def test_exit_above_sparse_observed_peak_is_not_misclassified_as_negative_giveback() -> None:
    state = start_bybit_demo_trade_excursion(_plan(), position=_position())
    state = observe_bybit_demo_trade_excursion(
        state,
        position=_position(unrealised="10"),
        quote=_quote("105", server_time_ms=1_000),
    )

    final = finalize_bybit_demo_trade_excursion(
        state,
        trade=_terminal_trade(exit_price="108"),
    )

    assert final.observed_peak_favorable_r == Decimal("1")
    assert final.realized_gross_exit_r == Decimal("1.6")
    assert final.exit_exceeded_observed_peak is True
    assert final.giveback_from_observed_peak_to_exit_r == Decimal("0")


def test_excursion_quality_is_diagnostics_only_and_never_authorizes_retuning() -> None:
    state = start_bybit_demo_trade_excursion(_plan(), position=_position())
    state = observe_bybit_demo_trade_excursion(
        state,
        position=_position(unrealised="20"),
        quote=_quote("110", server_time_ms=1_000),
    )
    final = finalize_bybit_demo_trade_excursion(
        state,
        trade=_terminal_trade(exit_price="102"),
    )

    quality = summarize_bybit_demo_excursion_quality((final,))

    assert quality["trade_count"] == 1
    assert quality["trades_with_excursion_observations"] == 1
    assert quality["average_observed_peak_favorable_r"] == 2.0
    assert quality["average_giveback_from_observed_peak_to_exit_r"] == 1.6
    assert quality["observed_peak_is_sampling_lower_bound"] is True
    assert quality["diagnostics_only"] is True
    assert quality["exit_threshold_retuning_allowed"] is False
    assert quality["strategy_promotion_allowed"] is False
    assert quality["live_mainnet_order_routing_allowed"] is False
