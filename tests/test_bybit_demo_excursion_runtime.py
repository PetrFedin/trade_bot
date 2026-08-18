from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.execution.bybit_demo import BybitDemoOrderAck, BybitDemoPosition
from app.execution.bybit_demo_excursion_runtime import (
    BybitDemoExcursionRuntimeStatus,
    acknowledge_bybit_demo_excursion_final,
    advance_bybit_demo_excursion_tracking,
    initialize_bybit_demo_excursion_from_strategy_cycle,
)
from app.execution.bybit_demo_excursion_store import JsonFileBybitDemoExcursionStore
from app.execution.bybit_demo_strategy_selector import BybitDemoStrategyCycleStatus
from app.marketdata.bybit_demo_quotes import BybitDemoMarketQuote
from app.strategy.crypto_perp import CryptoSide, CryptoTradePlan


def _plan() -> CryptoTradePlan:
    return CryptoTradePlan(
        symbol="BTCUSDT",
        side=CryptoSide.LONG,
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


def _position(*, size: str = "2") -> BybitDemoPosition:
    return BybitDemoPosition(
        symbol="BTCUSDT",
        side="Buy",
        size=Decimal(size),
        average_price=Decimal("100"),
        unrealised_pnl=Decimal("0"),
        liquidation_price=Decimal("50"),
    )


def _protected_strategy_cycle():
    cycle = SimpleNamespace(
        status=SimpleNamespace(value="PROTECTED"),
        entry_ack=BybitDemoOrderAck(
            order_id="entry-1",
            order_link_id="ASTRA-DEMO-E-EXCURSION",
            accepted=True,
        ),
        reconciled_position=_position(),
    )
    return SimpleNamespace(
        live_mainnet_order_routing_allowed=False,
        status=BybitDemoStrategyCycleStatus.GUARDED_ORCHESTRATOR_CALLED,
        orchestrator_result=SimpleNamespace(cycle_result=cycle),
        selection=SimpleNamespace(selected_trade_plan=_plan()),
    )


def _entry_fill() -> dict[str, str]:
    return {
        "symbol": "BTCUSDT",
        "execId": "entry-fill",
        "orderLinkId": "ASTRA-DEMO-E-EXCURSION",
        "side": "Buy",
        "execQty": "2",
        "execPrice": "100",
        "execFee": "0.1",
        "execTime": "1000",
    }


def _exit_fill(*, price: str = "102") -> dict[str, str]:
    return {
        "symbol": "BTCUSDT",
        "execId": "exit-fill",
        "orderLinkId": "ASTRA-DEMO-X-EXCURSION",
        "side": "Sell",
        "execQty": "2",
        "execPrice": price,
        "execFee": "0.1",
        "execTime": "2000",
    }


class _TradeClient:
    live_mainnet_order_routing_allowed = False

    def __init__(self, *, terminal: bool = False, remaining: str = "2") -> None:
        self.terminal = terminal
        self.remaining = Decimal(remaining)

    def get_executions(
        self,
        *,
        symbol: str,
        order_link_id: str | None = None,
        limit: int = 50,
    ):
        assert symbol == "BTCUSDT"
        assert 1 <= limit <= 100
        if order_link_id is not None:
            return (_entry_fill(),)
        if self.terminal:
            return (_entry_fill(), _exit_fill())
        return (_entry_fill(),)

    def get_positions(self, *, settle_coin: str = "USDT"):
        assert settle_coin == "USDT"
        if self.terminal:
            return ()
        return (_position(size=str(self.remaining)),)


class _QuoteClient:
    live_mainnet_order_routing_allowed = False

    def __init__(self, mark: str = "110") -> None:
        self.mark = Decimal(mark)

    def get_quote(self, *, symbol: str) -> BybitDemoMarketQuote:
        assert symbol == "BTCUSDT"
        return BybitDemoMarketQuote(
            symbol=symbol,
            last_price=self.mark,
            mark_price=self.mark,
            bid_price=self.mark - Decimal("0.01"),
            ask_price=self.mark + Decimal("0.01"),
            server_time_ms=10_000,
            received_time_ms=10_100,
            age_ms=100,
        )


def test_protected_cycle_initializes_persistent_excursion_baseline(tmp_path) -> None:
    store = JsonFileBybitDemoExcursionStore(tmp_path / "excursion.json")

    result = initialize_bybit_demo_excursion_from_strategy_cycle(
        _protected_strategy_cycle(),
        store=store,
    )

    assert result.status is BybitDemoExcursionRuntimeStatus.TRACKING_INITIALIZED
    assert result.checkpoint is not None
    assert result.checkpoint.entry_order_link_id == "ASTRA-DEMO-E-EXCURSION"
    assert result.checkpoint.state.entry_price == Decimal("100")
    assert result.checkpoint.state.initial_quantity == Decimal("2")
    assert result.checkpoint.state.observed_peak_favorable_r == Decimal("0")


def test_open_poll_reconciles_trade_and_persists_observed_peak(tmp_path) -> None:
    store = JsonFileBybitDemoExcursionStore(tmp_path / "excursion.json")
    initialize_bybit_demo_excursion_from_strategy_cycle(
        _protected_strategy_cycle(),
        store=store,
    )

    result = advance_bybit_demo_excursion_tracking(
        store=store,
        trade_client=_TradeClient(),
        quote_client=_QuoteClient("110"),
    )

    assert result.status is BybitDemoExcursionRuntimeStatus.OPEN_OBSERVED
    assert result.trade is not None
    assert result.trade.terminal is False
    assert result.checkpoint is not None
    assert result.checkpoint.state.observation_count == 1
    assert result.checkpoint.state.observed_peak_favorable_r == Decimal("2")
    assert store.load().revision == result.checkpoint.revision


def test_partial_close_observation_keeps_initial_basis(tmp_path) -> None:
    store = JsonFileBybitDemoExcursionStore(tmp_path / "excursion.json")
    initialize_bybit_demo_excursion_from_strategy_cycle(
        _protected_strategy_cycle(),
        store=store,
    )

    result = advance_bybit_demo_excursion_tracking(
        store=store,
        trade_client=_TradeClient(remaining="1"),
        quote_client=_QuoteClient("105"),
    )

    assert result.status is BybitDemoExcursionRuntimeStatus.TRACKING_BLOCKED
    assert result.trade is not None
    assert "POSITION_AND_EXECUTION_QUANTITY_MISMATCH" in result.trade.reasons


def test_terminal_evidence_is_two_phase_and_checkpoint_survives_until_ack(tmp_path) -> None:
    store = JsonFileBybitDemoExcursionStore(tmp_path / "excursion.json")
    initialize_bybit_demo_excursion_from_strategy_cycle(
        _protected_strategy_cycle(),
        store=store,
    )
    observed = advance_bybit_demo_excursion_tracking(
        store=store,
        trade_client=_TradeClient(),
        quote_client=_QuoteClient("110"),
    )
    assert observed.checkpoint is not None

    terminal = advance_bybit_demo_excursion_tracking(
        store=store,
        trade_client=_TradeClient(terminal=True),
        quote_client=_QuoteClient("102"),
    )

    assert terminal.status is BybitDemoExcursionRuntimeStatus.TERMINAL_EVIDENCE_READY
    assert terminal.final is not None
    assert terminal.final.observed_peak_favorable_r == Decimal("2")
    assert terminal.final.realized_gross_exit_r == Decimal("0.4")
    assert terminal.final.giveback_from_observed_peak_to_exit_r == Decimal("1.6")
    assert terminal.checkpoint_clear_allowed is True
    assert terminal.checkpoint is not None
    assert store.load().revision == terminal.checkpoint.revision

    acknowledged = acknowledge_bybit_demo_excursion_final(
        store=store,
        expected_revision=terminal.checkpoint.revision,
    )
    assert acknowledged.status is BybitDemoExcursionRuntimeStatus.FINAL_ACKNOWLEDGED
    with pytest.raises(FileNotFoundError):
        store.load()


def test_missing_checkpoint_blocks_without_reconstructing_zero_peak(tmp_path) -> None:
    store = JsonFileBybitDemoExcursionStore(tmp_path / "missing.json")

    result = advance_bybit_demo_excursion_tracking(
        store=store,
        trade_client=_TradeClient(),
        quote_client=_QuoteClient(),
    )

    assert result.status is BybitDemoExcursionRuntimeStatus.TRACKING_BLOCKED
    assert result.reasons == ("EXCURSION_CHECKPOINT_LOAD_FAILED:FileNotFoundError",)


def test_quote_failure_keeps_existing_checkpoint_unchanged(tmp_path) -> None:
    class BrokenQuoteClient:
        live_mainnet_order_routing_allowed = False

        def get_quote(self, *, symbol: str):
            raise TimeoutError(symbol)

    store = JsonFileBybitDemoExcursionStore(tmp_path / "excursion.json")
    initialized = initialize_bybit_demo_excursion_from_strategy_cycle(
        _protected_strategy_cycle(),
        store=store,
    )
    assert initialized.checkpoint is not None

    result = advance_bybit_demo_excursion_tracking(
        store=store,
        trade_client=_TradeClient(),
        quote_client=BrokenQuoteClient(),
    )

    assert result.status is BybitDemoExcursionRuntimeStatus.TRACKING_BLOCKED
    assert result.reasons == ("EXCURSION_OBSERVATION_FAILED:TimeoutError",)
    assert store.load().revision == initialized.checkpoint.revision


def test_runtime_rejects_mainnet_capable_read_dependency(tmp_path) -> None:
    class UnsafeQuoteClient:
        live_mainnet_order_routing_allowed = True

    store = JsonFileBybitDemoExcursionStore(tmp_path / "excursion.json")
    initialize_bybit_demo_excursion_from_strategy_cycle(
        _protected_strategy_cycle(),
        store=store,
    )

    with pytest.raises(ValueError, match="mainnet-capable quote client"):
        advance_bybit_demo_excursion_tracking(
            store=store,
            trade_client=_TradeClient(),
            quote_client=UnsafeQuoteClient(),
        )
