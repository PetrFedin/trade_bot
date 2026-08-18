from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from app.execution.bybit_demo import BybitDemoFeeRate
from app.execution.bybit_demo_excursion_store import JsonFileBybitDemoExcursionStore
from app.execution.bybit_demo_excursion_tracker import BybitDemoTradeExcursionState
from app.execution.bybit_demo_protection_client import BybitDemoProtectionPosition
from app.execution.bybit_demo_stop_ratchet_client import (
    BybitDemoStopRatchetAck,
    BybitDemoStopRatchetRequest,
)
from app.execution.bybit_demo_trade_management_runtime import (
    BybitDemoTradeManagementRuntimePolicy,
    BybitDemoTradeManagementRuntimeStatus,
    run_bybit_demo_trade_management_cycle,
)
from app.marketdata.bybit_demo_quotes import BybitDemoMarketQuote
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.marketdata.bybit_v5 import BybitKlineBar
from app.strategy.crypto_perp import CryptoPerpStrategyConfig, CryptoSide

_INTERVAL_MS = 5 * 60 * 1000
_ENTRY_BUCKET = 10 * _INTERVAL_MS
_ENTRY_TIME = _ENTRY_BUCKET + 60_000


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


def _state() -> BybitDemoTradeExcursionState:
    return BybitDemoTradeExcursionState(
        symbol="BTCUSDT",
        side=CryptoSide.LONG,
        entry_price=Decimal("100"),
        initial_quantity=Decimal("2"),
        stop_fraction=Decimal("0.05"),
        current_quantity=Decimal("2"),
    )


def _position(*, stop: str = "95") -> BybitDemoProtectionPosition:
    return BybitDemoProtectionPosition(
        symbol="BTCUSDT",
        side="Buy",
        size=Decimal("2"),
        average_price=Decimal("100"),
        unrealised_pnl=Decimal("0"),
        liquidation_price=Decimal("50"),
        take_profit_price=Decimal("112"),
        stop_loss_price=Decimal(stop),
        trailing_stop_distance=None,
    )


def _bar(start_ms: int, *, high: str = "104") -> BybitKlineBar:
    return BybitKlineBar(
        symbol="BTCUSDT",
        start_time=datetime.fromtimestamp(start_ms / 1000, tz=UTC),
        open=Decimal("100"),
        high=Decimal(high),
        low=Decimal("99"),
        close=Decimal("103"),
        volume=Decimal("10"),
        turnover=Decimal("1000"),
    )


def _store(tmp_path) -> JsonFileBybitDemoExcursionStore:
    store = JsonFileBybitDemoExcursionStore(tmp_path / "excursion.json")
    store.initialize(
        entry_order_link_id="ASTRA-DEMO-E-MANAGEMENT",
        state=_state(),
    )
    return store


class _BarClient:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(self, bars: tuple[BybitKlineBar, ...]) -> None:
        self.bars = bars
        self.calls: list[tuple[str, int, int, str]] = []

    def fetch_completed_range(
        self,
        *,
        symbol: str,
        start_ms: int,
        now_ms: int,
        interval: str = "5",
    ) -> tuple[BybitKlineBar, ...]:
        self.calls.append((symbol, start_ms, now_ms, interval))
        return self.bars


class _QuoteClient:
    live_mainnet_order_routing_allowed = False

    def __init__(self, last: str = "105") -> None:
        self.last = Decimal(last)

    def get_quote(self, *, symbol: str) -> BybitDemoMarketQuote:
        return BybitDemoMarketQuote(
            symbol=symbol,
            last_price=self.last,
            mark_price=self.last,
            bid_price=self.last - Decimal("0.01"),
            ask_price=self.last + Decimal("0.01"),
            server_time_ms=10_000,
            received_time_ms=10_100,
            age_ms=100,
        )


class _Client:
    live_mainnet_order_routing_allowed = False
    stop_ratchet_write_supported = True

    def __init__(
        self,
        *,
        position: BybitDemoProtectionPosition | None = None,
        reflect_write: bool = True,
        prewrite_stop: str | None = None,
    ) -> None:
        self.position = _position() if position is None else position
        self.reflect_write = reflect_write
        self.prewrite_stop = prewrite_stop
        self.position_reads = 0
        self.ratchet_requests: list[BybitDemoStopRatchetRequest] = []

    def get_executions(
        self,
        *,
        symbol: str,
        order_link_id: str | None = None,
        limit: int = 50,
    ):
        assert symbol == "BTCUSDT"
        assert order_link_id == "ASTRA-DEMO-E-MANAGEMENT"
        assert limit == 100
        return (
            {
                "symbol": "BTCUSDT",
                "execId": "entry-1",
                "orderLinkId": "ASTRA-DEMO-E-MANAGEMENT",
                "side": "Buy",
                "execQty": "2",
                "execPrice": "100",
                "execFee": "0.12",
                "execTime": str(_ENTRY_TIME),
            },
        )

    def get_fee_rate(self, *, symbol: str) -> BybitDemoFeeRate:
        assert symbol == "BTCUSDT"
        return BybitDemoFeeRate(
            symbol=symbol,
            taker_fee_rate=Decimal("0.0006"),
            maker_fee_rate=Decimal("0.0001"),
        )

    def get_positions(self, *, settle_coin: str = "USDT"):
        assert settle_coin == "USDT"
        self.position_reads += 1
        if self.position is None:
            return ()
        if self.position_reads == 2 and self.prewrite_stop is not None:
            self.position = replace(
                self.position,
                stop_loss_price=Decimal(self.prewrite_stop),
            )
        return (self.position,)

    def ratchet_position_stop_loss(
        self,
        request: BybitDemoStopRatchetRequest,
    ) -> BybitDemoStopRatchetAck:
        self.ratchet_requests.append(request)
        if self.reflect_write and self.position is not None:
            self.position = replace(
                self.position,
                stop_loss_price=request.new_stop_loss_price,
            )
        return BybitDemoStopRatchetAck(
            symbol=request.symbol,
            previous_stop_loss_price=request.previous_stop_loss_price,
            stop_loss_price=request.new_stop_loss_price,
        )


def _now_after_one_full_post_entry_bar() -> int:
    return _ENTRY_BUCKET + 2 * _INTERVAL_MS + 10_000


def test_shadow_runtime_excludes_entry_bucket_and_reports_break_even_due(tmp_path) -> None:
    bar_client = _BarClient((_bar(_ENTRY_BUCKET + _INTERVAL_MS),))
    client = _Client()

    result = run_bybit_demo_trade_management_cycle(
        excursion_store=_store(tmp_path),
        client=client,
        completed_bar_client=bar_client,
        quote_client=_QuoteClient(),
        instrument=_instrument(),
        strategy_config=CryptoPerpStrategyConfig(),
        now_ms=_now_after_one_full_post_entry_bar(),
    )

    assert result.status is BybitDemoTradeManagementRuntimeStatus.SHADOW_RATCHET_DUE
    assert result.entry_execution_time_ms == _ENTRY_TIME
    assert result.entry_bucket_start_ms == _ENTRY_BUCKET
    assert result.protection_bar_start_ms == _ENTRY_BUCKET + _INTERVAL_MS
    assert bar_client.calls[0][1] == _ENTRY_BUCKET + _INTERVAL_MS
    assert result.decision is not None
    assert result.decision.completed_bar_count == 1
    assert result.decision.holding_bar_count == 2
    assert result.decision.actual_entry_fee_used is True
    assert result.actual_entry_fee_usdt == Decimal("0.12")
    assert result.stop_ratchet_write_attempted is False
    assert client.ratchet_requests == []


def test_explicit_stop_ratchet_is_rechecked_and_exchange_verified(tmp_path) -> None:
    client = _Client()
    result = run_bybit_demo_trade_management_cycle(
        excursion_store=_store(tmp_path),
        client=client,
        completed_bar_client=_BarClient((_bar(_ENTRY_BUCKET + _INTERVAL_MS),)),
        quote_client=_QuoteClient("105"),
        instrument=_instrument(),
        strategy_config=CryptoPerpStrategyConfig(),
        now_ms=_now_after_one_full_post_entry_bar(),
        runtime_policy=BybitDemoTradeManagementRuntimePolicy(
            stop_ratchet_writes_enabled=True
        ),
    )

    assert result.status is BybitDemoTradeManagementRuntimeStatus.RATCHET_VERIFIED
    assert result.stop_ratchet_write_attempted is True
    assert result.stop_ratchet_verified is True
    assert result.ratchet_ack is not None
    assert len(client.ratchet_requests) == 1
    request = client.ratchet_requests[0]
    assert request.previous_stop_loss_price == Decimal("95")
    assert request.new_stop_loss_price > Decimal("100")
    assert request.current_last_price == Decimal("105")
    assert result.post_write_position is not None
    assert result.post_write_position.stop_loss_price == request.new_stop_loss_price
    assert result.post_write_position.take_profit_price == Decimal("112")
    assert result.post_write_position.trailing_stop_distance is None


def test_ack_without_exchange_stop_change_is_unverified_not_success(tmp_path) -> None:
    client = _Client(reflect_write=False)
    result = run_bybit_demo_trade_management_cycle(
        excursion_store=_store(tmp_path),
        client=client,
        completed_bar_client=_BarClient((_bar(_ENTRY_BUCKET + _INTERVAL_MS),)),
        quote_client=_QuoteClient(),
        instrument=_instrument(),
        strategy_config=CryptoPerpStrategyConfig(),
        now_ms=_now_after_one_full_post_entry_bar(),
        runtime_policy=BybitDemoTradeManagementRuntimePolicy(
            stop_ratchet_writes_enabled=True
        ),
    )

    assert result.status is BybitDemoTradeManagementRuntimeStatus.RATCHET_UNVERIFIED
    assert result.stop_ratchet_write_attempted is True
    assert result.stop_ratchet_verified is False
    assert "MANAGEMENT_STOP_RATCHET_NOT_REFLECTED" in result.reasons


def test_fresh_market_already_through_desired_stop_blocks_write(tmp_path) -> None:
    client = _Client()
    result = run_bybit_demo_trade_management_cycle(
        excursion_store=_store(tmp_path),
        client=client,
        completed_bar_client=_BarClient((_bar(_ENTRY_BUCKET + _INTERVAL_MS),)),
        quote_client=_QuoteClient("100"),
        instrument=_instrument(),
        strategy_config=CryptoPerpStrategyConfig(),
        now_ms=_now_after_one_full_post_entry_bar(),
        runtime_policy=BybitDemoTradeManagementRuntimePolicy(
            stop_ratchet_writes_enabled=True
        ),
    )

    assert result.status is BybitDemoTradeManagementRuntimeStatus.RATCHET_WINDOW_MISSED
    assert result.stop_ratchet_write_attempted is False
    assert client.ratchet_requests == []


def test_concurrent_more_protective_stop_makes_write_unnecessary(tmp_path) -> None:
    client = _Client(prewrite_stop="102")
    result = run_bybit_demo_trade_management_cycle(
        excursion_store=_store(tmp_path),
        client=client,
        completed_bar_client=_BarClient((_bar(_ENTRY_BUCKET + _INTERVAL_MS),)),
        quote_client=_QuoteClient(),
        instrument=_instrument(),
        strategy_config=CryptoPerpStrategyConfig(),
        now_ms=_now_after_one_full_post_entry_bar(),
        runtime_policy=BybitDemoTradeManagementRuntimePolicy(
            stop_ratchet_writes_enabled=True
        ),
    )

    assert result.status is BybitDemoTradeManagementRuntimeStatus.NO_CHANGE
    assert result.reasons == ("MANAGEMENT_STOP_ALREADY_SATISFIES_BASELINE",)
    assert client.ratchet_requests == []


def test_max_hold_uses_entry_bucket_for_time_but_not_protection_extrema(tmp_path) -> None:
    protection_bars = tuple(
        _bar(_ENTRY_BUCKET + index * _INTERVAL_MS, high="101")
        for index in range(1, 36)
    )
    result = run_bybit_demo_trade_management_cycle(
        excursion_store=_store(tmp_path),
        client=_Client(),
        completed_bar_client=_BarClient(protection_bars),
        quote_client=_QuoteClient(),
        instrument=_instrument(),
        strategy_config=CryptoPerpStrategyConfig(),
        now_ms=_ENTRY_BUCKET + 36 * _INTERVAL_MS + 1_000,
    )

    assert result.status is BybitDemoTradeManagementRuntimeStatus.MAX_HOLD_CLOSE_REQUIRED
    assert result.decision is not None
    assert result.decision.completed_bar_count == 35
    assert result.decision.holding_bar_count == 36
    assert result.decision.maximum_favorable_r == Decimal("0.2")
    assert result.max_hold_close_write_allowed is False
    assert result.stop_ratchet_write_attempted is False


def test_closed_position_is_not_treated_as_management_failure(tmp_path) -> None:
    client = _Client()
    client.position = None
    result = run_bybit_demo_trade_management_cycle(
        excursion_store=_store(tmp_path),
        client=client,
        completed_bar_client=_BarClient((_bar(_ENTRY_BUCKET + _INTERVAL_MS),)),
        quote_client=_QuoteClient(),
        instrument=_instrument(),
        strategy_config=CryptoPerpStrategyConfig(),
        now_ms=_now_after_one_full_post_entry_bar(),
    )

    assert result.status is BybitDemoTradeManagementRuntimeStatus.POSITION_CLOSED
    assert result.stop_ratchet_write_attempted is False
