from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from app.execution.bybit_demo import BybitDemoOrderAck
from app.execution.bybit_demo_excursion_store import JsonFileBybitDemoExcursionStore
from app.execution.bybit_demo_excursion_tracker import BybitDemoTradeExcursionState
from app.execution.bybit_demo_max_hold_close import (
    BybitDemoMaxHoldClosePolicy,
    BybitDemoMaxHoldCloseStatus,
    execute_bybit_demo_max_hold_close,
)
from app.execution.bybit_demo_protection_client import BybitDemoProtectionPosition
from app.execution.bybit_demo_trade_management_runtime import (
    BybitDemoTradeManagementRuntimeStatus,
)
from app.marketdata.bybit_demo_quotes import BybitDemoMarketQuote
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.strategy.crypto_perp import CryptoSide


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


def _store(tmp_path) -> JsonFileBybitDemoExcursionStore:
    store = JsonFileBybitDemoExcursionStore(tmp_path / "excursion.json")
    store.initialize(
        entry_order_link_id="ASTRA-DEMO-E-MAX-HOLD",
        state=BybitDemoTradeExcursionState(
            symbol="BTCUSDT",
            side=CryptoSide.LONG,
            entry_price=Decimal("100"),
            initial_quantity=Decimal("2"),
            stop_fraction=Decimal("0.05"),
            current_quantity=Decimal("2"),
        ),
    )
    return store


def _management(*, due: bool = True):
    return SimpleNamespace(
        status=(
            BybitDemoTradeManagementRuntimeStatus.MAX_HOLD_CLOSE_REQUIRED
            if due
            else BybitDemoTradeManagementRuntimeStatus.NO_CHANGE
        ),
        decision=SimpleNamespace(max_hold_close_required=due),
        live_mainnet_order_routing_allowed=False,
    )


def _position(*, size: str = "2") -> BybitDemoProtectionPosition:
    return BybitDemoProtectionPosition(
        symbol="BTCUSDT",
        side="Buy",
        size=Decimal(size),
        average_price=Decimal("100"),
        unrealised_pnl=Decimal("0"),
        liquidation_price=Decimal("50"),
        take_profit_price=Decimal("112"),
        stop_loss_price=Decimal("101.7"),
        trailing_stop_distance=None,
    )


class _QuoteClient:
    live_mainnet_order_routing_allowed = False

    def get_quote(self, *, symbol: str) -> BybitDemoMarketQuote:
        return BybitDemoMarketQuote(
            symbol=symbol,
            last_price=Decimal("103"),
            mark_price=Decimal("103"),
            bid_price=Decimal("102.9"),
            ask_price=Decimal("103.1"),
            server_time_ms=1_000,
            received_time_ms=1_100,
            age_ms=100,
        )


class _Client:
    live_mainnet_order_routing_allowed = False

    def __init__(
        self,
        *,
        size: str = "2",
        close_after_write: bool = True,
        position_read_fail_after_write: bool = False,
    ) -> None:
        self.position = _position(size=size)
        self.close_after_write = close_after_write
        self.position_read_fail_after_write = position_read_fail_after_write
        self.write_count = 0
        self.requests: list[object] = []

    def get_positions(self, *, settle_coin: str = "USDT"):
        assert settle_coin == "USDT"
        if self.write_count > 0 and self.position_read_fail_after_write:
            raise TimeoutError("position")
        if self.position is None:
            return ()
        return (self.position,)

    def place_market_order(self, request: object) -> BybitDemoOrderAck:
        self.requests.append(request)
        self.write_count += 1
        if self.close_after_write:
            self.position = None
        return BybitDemoOrderAck(
            order_id="max-hold-close-1",
            order_link_id=request.order_link_id,
            accepted=True,
        )


def test_max_hold_close_is_shadow_only_by_default(tmp_path) -> None:
    client = _Client()

    result = execute_bybit_demo_max_hold_close(
        _management(),
        excursion_store=_store(tmp_path),
        client=client,
        quote_client=_QuoteClient(),
        instrument=_instrument(),
    )

    assert result.status is BybitDemoMaxHoldCloseStatus.WRITES_DISABLED
    assert client.write_count == 0
    assert result.next_entry_allowed is False
    assert result.lifecycle_reconciliation_still_required is True


def test_explicit_max_hold_close_requires_position_disappearance_not_ack(tmp_path) -> None:
    client = _Client(close_after_write=True)

    result = execute_bybit_demo_max_hold_close(
        _management(),
        excursion_store=_store(tmp_path),
        client=client,
        quote_client=_QuoteClient(),
        instrument=_instrument(),
        policy=BybitDemoMaxHoldClosePolicy(
            writes_enabled=True,
            reconciliation_attempts=2,
            reconciliation_delay_seconds=0,
        ),
    )

    assert result.status is BybitDemoMaxHoldCloseStatus.CLOSE_CONFIRMED
    assert result.position_closed is True
    assert result.residual_size == Decimal("0")
    assert result.reconciliation_attempts == 1
    assert result.close_request is not None
    assert result.close_request.reduce_only is True
    assert result.close_request.side == "Sell"
    assert result.close_request.quantity == Decimal("2")
    assert result.close_request.order_link_id.startswith("ASTRA-DEMO-H-")
    assert len(result.close_request.order_link_id) <= 36
    assert result.close_ack is not None
    assert result.next_entry_allowed is False


def test_ack_with_residual_position_is_unresolved(tmp_path) -> None:
    client = _Client(close_after_write=False)

    result = execute_bybit_demo_max_hold_close(
        _management(),
        excursion_store=_store(tmp_path),
        client=client,
        quote_client=_QuoteClient(),
        instrument=_instrument(),
        policy=BybitDemoMaxHoldClosePolicy(
            writes_enabled=True,
            reconciliation_attempts=3,
            reconciliation_delay_seconds=0,
        ),
    )

    assert result.status is BybitDemoMaxHoldCloseStatus.CLOSE_UNRESOLVED
    assert result.position_closed is False
    assert result.residual_size == Decimal("2")
    assert result.reconciliation_attempts == 3
    assert result.reasons == ("MAX_HOLD_RESIDUAL_POSITION",)
    assert client.write_count == 1


def test_unreadable_post_close_position_is_not_treated_as_closed(tmp_path) -> None:
    client = _Client(
        close_after_write=False,
        position_read_fail_after_write=True,
    )

    result = execute_bybit_demo_max_hold_close(
        _management(),
        excursion_store=_store(tmp_path),
        client=client,
        quote_client=_QuoteClient(),
        instrument=_instrument(),
        policy=BybitDemoMaxHoldClosePolicy(
            writes_enabled=True,
            reconciliation_attempts=2,
            reconciliation_delay_seconds=0,
        ),
    )

    assert result.status is BybitDemoMaxHoldCloseStatus.CLOSE_UNRESOLVED
    assert result.position_closed is False
    assert result.residual_size is None
    assert result.reasons == ("MAX_HOLD_POST_CLOSE_POSITION_READ_FAILED:TimeoutError",)


def test_position_size_drift_blocks_close_before_order(tmp_path) -> None:
    client = _Client(size="1")

    result = execute_bybit_demo_max_hold_close(
        _management(),
        excursion_store=_store(tmp_path),
        client=client,
        quote_client=_QuoteClient(),
        instrument=_instrument(),
        policy=BybitDemoMaxHoldClosePolicy(writes_enabled=True),
    )

    assert result.status is BybitDemoMaxHoldCloseStatus.CLOSE_BLOCKED
    assert result.reasons == ("MAX_HOLD_POSITION_SIZE_CHANGED_FROM_BASELINE",)
    assert result.residual_size == Decimal("1")
    assert client.write_count == 0


def test_non_due_management_result_never_writes(tmp_path) -> None:
    client = _Client()

    result = execute_bybit_demo_max_hold_close(
        _management(due=False),
        excursion_store=_store(tmp_path),
        client=client,
        quote_client=_QuoteClient(),
        instrument=_instrument(),
        policy=BybitDemoMaxHoldClosePolicy(writes_enabled=True),
    )

    assert result.status is BybitDemoMaxHoldCloseStatus.NOT_DUE
    assert client.write_count == 0
