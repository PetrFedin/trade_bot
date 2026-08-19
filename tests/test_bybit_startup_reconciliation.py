from __future__ import annotations

from decimal import Decimal

import pytest

from app.execution.bybit_demo import BybitDemoPosition
from app.execution.bybit_demo_excursion_store import BybitDemoExcursionCheckpoint
from app.execution.bybit_demo_excursion_tracker import BybitDemoTradeExcursionState
from app.execution.bybit_startup_reconciliation import (
    BybitStartupReconciliationStatus,
    reconcile_bybit_startup,
)
from app.strategy.crypto_perp import CryptoSide


class _CheckpointStore:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(self, checkpoint: BybitDemoExcursionCheckpoint | None) -> None:
        self.checkpoint = checkpoint

    def load(self) -> BybitDemoExcursionCheckpoint:
        if self.checkpoint is None:
            raise FileNotFoundError
        return self.checkpoint


class _Broker:
    live_mainnet_order_routing_allowed = False

    def __init__(
        self,
        *,
        positions: tuple[BybitDemoPosition, ...] = (),
        open_orders: tuple[dict[str, object], ...] = (),
        fail_reads: bool = False,
    ) -> None:
        self.positions = positions
        self.open_orders = open_orders
        self.fail_reads = fail_reads

    def get_positions(self, *, settle_coin: str = "USDT") -> tuple[BybitDemoPosition, ...]:
        assert settle_coin == "USDT"
        if self.fail_reads:
            raise TimeoutError("broker unavailable")
        return self.positions

    def get_open_orders(
        self,
        *,
        settle_coin: str = "USDT",
        limit: int = 50,
    ) -> tuple[dict[str, object], ...]:
        assert settle_coin == "USDT"
        assert limit == 50
        if self.fail_reads:
            raise TimeoutError("broker unavailable")
        return self.open_orders


def _checkpoint(
    *,
    side: CryptoSide = CryptoSide.LONG,
    symbol: str = "BTCUSDT",
    quantity: str = "1",
) -> BybitDemoExcursionCheckpoint:
    state = BybitDemoTradeExcursionState(
        symbol=symbol,
        side=side,
        entry_price=Decimal("100"),
        initial_quantity=Decimal(quantity),
        stop_fraction=Decimal("0.01"),
        current_quantity=Decimal(quantity),
    )
    checkpoint = BybitDemoExcursionCheckpoint(
        entry_order_link_id="ASTRA-DEMO-ENTRY-1",
        state=state,
        revision="a" * 64,
    )
    checkpoint.validate()
    return checkpoint


def _position(
    *,
    side: str = "Buy",
    symbol: str = "BTCUSDT",
    size: str = "1",
) -> BybitDemoPosition:
    return BybitDemoPosition(
        symbol=symbol,
        side=side,
        size=Decimal(size),
        average_price=Decimal("100"),
        unrealised_pnl=Decimal("0"),
        liquidation_price=Decimal("50"),
    )


def _order(*, order_link_id: str = "ASTRA-DEMO-ENTRY-1") -> dict[str, object]:
    return {
        "symbol": "BTCUSDT",
        "orderStatus": "New",
        "orderLinkId": order_link_id,
    }


def test_flat_broker_and_missing_checkpoint_is_only_entry_ready_state() -> None:
    result = reconcile_bybit_startup(
        broker=_Broker(),
        checkpoint_store=_CheckpointStore(None),
    )

    assert result.status is BybitStartupReconciliationStatus.READY_FOR_ENTRY
    assert result.next_entry_allowed is True
    assert result.management_allowed is False
    assert result.broker_truth_complete is True
    assert result.live_mainnet_order_routing_allowed is False


def test_long_checkpoint_and_matching_position_resume_management() -> None:
    result = reconcile_bybit_startup(
        broker=_Broker(positions=(_position(side="Buy"),)),
        checkpoint_store=_CheckpointStore(_checkpoint(side=CryptoSide.LONG)),
    )

    assert result.status is BybitStartupReconciliationStatus.RESUME_MANAGEMENT
    assert result.management_allowed is True
    assert result.next_entry_allowed is False


def test_short_checkpoint_and_matching_position_resume_management() -> None:
    result = reconcile_bybit_startup(
        broker=_Broker(positions=(_position(side="Sell"),)),
        checkpoint_store=_CheckpointStore(_checkpoint(side=CryptoSide.SHORT)),
    )

    assert result.status is BybitStartupReconciliationStatus.RESUME_MANAGEMENT
    assert result.management_allowed is True
    assert result.next_entry_allowed is False


def test_restart_with_broker_position_but_no_checkpoint_blocks_new_entry() -> None:
    result = reconcile_bybit_startup(
        broker=_Broker(positions=(_position(),)),
        checkpoint_store=_CheckpointStore(None),
    )

    assert result.status is BybitStartupReconciliationStatus.BLOCKED
    assert result.reasons == ("BROKER_POSITION_WITHOUT_ACTIVE_CHECKPOINT",)
    assert result.next_entry_allowed is False


def test_restart_with_open_order_but_no_checkpoint_blocks_new_entry() -> None:
    result = reconcile_bybit_startup(
        broker=_Broker(open_orders=(_order(),)),
        checkpoint_store=_CheckpointStore(None),
    )

    assert result.status is BybitStartupReconciliationStatus.BLOCKED
    assert result.reasons == ("BROKER_OPEN_ORDER_WITHOUT_ACTIVE_CHECKPOINT",)


def test_closed_broker_position_with_checkpoint_requires_terminal_recovery() -> None:
    result = reconcile_bybit_startup(
        broker=_Broker(),
        checkpoint_store=_CheckpointStore(_checkpoint()),
    )

    assert result.status is BybitStartupReconciliationStatus.TERMINAL_RECOVERY_REQUIRED
    assert result.terminal_recovery_required is True
    assert result.next_entry_allowed is False


def test_side_mismatch_blocks_for_long_and_short_safety() -> None:
    result = reconcile_bybit_startup(
        broker=_Broker(positions=(_position(side="Sell"),)),
        checkpoint_store=_CheckpointStore(_checkpoint(side=CryptoSide.LONG)),
    )

    assert result.status is BybitStartupReconciliationStatus.BLOCKED
    assert result.reasons == ("BROKER_POSITION_SIDE_MISMATCH",)


def test_broker_position_cannot_exceed_checkpoint_initial_exposure() -> None:
    result = reconcile_bybit_startup(
        broker=_Broker(positions=(_position(size="1.1"),)),
        checkpoint_store=_CheckpointStore(_checkpoint(quantity="1")),
    )

    assert result.status is BybitStartupReconciliationStatus.BLOCKED
    assert result.reasons == ("BROKER_POSITION_SIZE_EXCEEDS_CHECKPOINT",)


def test_partial_close_smaller_than_initial_exposure_can_resume() -> None:
    result = reconcile_bybit_startup(
        broker=_Broker(positions=(_position(size="0.4"),)),
        checkpoint_store=_CheckpointStore(_checkpoint(quantity="1")),
    )

    assert result.status is BybitStartupReconciliationStatus.RESUME_MANAGEMENT


def test_foreign_astra_order_during_active_trade_blocks() -> None:
    result = reconcile_bybit_startup(
        broker=_Broker(
            positions=(_position(),),
            open_orders=(_order(order_link_id="ASTRA-DEMO-OTHER-1"),),
        ),
        checkpoint_store=_CheckpointStore(_checkpoint()),
    )

    assert result.status is BybitStartupReconciliationStatus.BLOCKED
    assert result.reasons == ("FOREIGN_ASTRA_OPEN_ORDER_DURING_ACTIVE_TRADE",)


def test_broker_truth_read_failure_cannot_authorize_entry() -> None:
    result = reconcile_bybit_startup(
        broker=_Broker(fail_reads=True),
        checkpoint_store=_CheckpointStore(None),
    )

    assert result.status is BybitStartupReconciliationStatus.BLOCKED
    assert result.reasons == ("STARTUP_BROKER_TRUTH_READ_FAILED:TimeoutError",)
    assert result.broker_truth_complete is False
    assert result.next_entry_allowed is False


def test_mainnet_capable_broker_is_hard_rejected() -> None:
    broker = _Broker()
    broker.live_mainnet_order_routing_allowed = True

    with pytest.raises(ValueError, match="mainnet-capable startup broker"):
        reconcile_bybit_startup(
            broker=broker,
            checkpoint_store=_CheckpointStore(None),
        )


def test_mainnet_capable_checkpoint_store_is_hard_rejected() -> None:
    store = _CheckpointStore(None)
    store.live_mainnet_order_routing_allowed = True

    with pytest.raises(ValueError, match="mainnet-capable startup checkpoint store"):
        reconcile_bybit_startup(broker=_Broker(), checkpoint_store=store)
