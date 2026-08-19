from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest

from app.execution.bybit_demo import BybitDemoPosition
from app.execution.bybit_demo_trade_monitor import (
    BybitDemoTradeMonitorStatus,
    reconcile_bybit_demo_trade,
)


class _FakeReadClient:
    live_mainnet_order_routing_allowed = False

    def __init__(
        self,
        *,
        executions: tuple[Mapping[str, Any], ...],
        positions: tuple[BybitDemoPosition, ...] = (),
    ) -> None:
        self.executions = executions
        self.positions = positions

    def get_positions(self, *, settle_coin: str = "USDT") -> tuple[BybitDemoPosition, ...]:
        assert settle_coin == "USDT"
        return self.positions

    def get_executions(
        self,
        *,
        symbol: str,
        order_link_id: str | None = None,
        limit: int = 50,
    ) -> tuple[Mapping[str, Any], ...]:
        assert symbol == "BTCUSDT"
        assert 1 <= limit <= 100
        if order_link_id is None:
            return self.executions
        return tuple(
            row for row in self.executions if row.get("orderLinkId") == order_link_id
        )


def _fill(
    *,
    exec_id: str,
    side: str,
    qty: str,
    price: str,
    fee: str,
    time_ms: int,
    order_link_id: str = "",
) -> dict[str, str]:
    return {
        "symbol": "BTCUSDT",
        "execId": exec_id,
        "orderLinkId": order_link_id,
        "side": side,
        "execQty": qty,
        "execPrice": price,
        "execFee": fee,
        "execTime": str(time_ms),
    }


def _position(size: str) -> BybitDemoPosition:
    return BybitDemoPosition(
        symbol="BTCUSDT",
        side="Buy",
        size=Decimal(size),
        average_price=Decimal("100"),
        unrealised_pnl=Decimal("0"),
    )


def test_closed_trade_reconciles_fill_level_net_and_unlocks_symbol() -> None:
    link = "ASTRA-DEMO-E-ABC123"
    client = _FakeReadClient(
        executions=(
            _fill(
                exec_id="entry-1",
                side="Buy",
                qty="1",
                price="100",
                fee="0.06",
                time_ms=1000,
                order_link_id=link,
            ),
            _fill(
                exec_id="exit-1",
                side="Sell",
                qty="1",
                price="103",
                fee="0.0618",
                time_ms=2000,
            ),
        )
    )

    result = reconcile_bybit_demo_trade(
        client=client,
        symbol="BTCUSDT",
        entry_side="Buy",
        entry_order_link_id=link,
    )

    assert result.status is BybitDemoTradeMonitorStatus.CLOSED_RECONCILED
    assert result.entry_quantity == Decimal("1")
    assert result.exit_quantity == Decimal("1")
    assert result.remaining_quantity == Decimal("0")
    assert result.average_entry_price == Decimal("100")
    assert result.average_exit_price == Decimal("103")
    assert result.execution_fees_usdt == Decimal("0.1218")
    assert result.realized_gross_pnl_usdt == Decimal("3")
    assert result.realized_net_pnl_after_execution_fees_usdt == Decimal("2.8782")
    assert result.terminal is True
    assert result.next_entry_allowed is True
    assert result.funding_reconciled is False
    assert result.account_closed_pnl_reconciled is False
    assert result.live_mainnet_order_routing_allowed is False


def test_partial_close_keeps_symbol_locked_and_prorates_entry_fee() -> None:
    link = "ASTRA-DEMO-E-ABC123"
    client = _FakeReadClient(
        executions=(
            _fill(
                exec_id="entry-1",
                side="Buy",
                qty="1",
                price="100",
                fee="0.06",
                time_ms=1000,
                order_link_id=link,
            ),
            _fill(
                exec_id="exit-1",
                side="Sell",
                qty="0.4",
                price="102",
                fee="0.02448",
                time_ms=2000,
            ),
        ),
        positions=(_position("0.6"),),
    )

    result = reconcile_bybit_demo_trade(
        client=client,
        symbol="BTCUSDT",
        entry_side="Buy",
        entry_order_link_id=link,
    )

    assert result.status is BybitDemoTradeMonitorStatus.PARTIALLY_CLOSED
    assert result.remaining_quantity == Decimal("0.6")
    assert result.execution_fees_usdt == Decimal("0.08448")
    assert result.realized_gross_pnl_usdt == Decimal("0.8")
    assert result.realized_net_pnl_after_execution_fees_usdt == Decimal("0.75152")
    assert result.next_entry_allowed is False
    assert result.terminal is False


def test_missing_exit_history_fails_closed_even_when_position_is_absent() -> None:
    link = "ASTRA-DEMO-E-ABC123"
    client = _FakeReadClient(
        executions=(
            _fill(
                exec_id="entry-1",
                side="Buy",
                qty="1",
                price="100",
                fee="0.06",
                time_ms=1000,
                order_link_id=link,
            ),
        )
    )

    result = reconcile_bybit_demo_trade(
        client=client,
        symbol="BTCUSDT",
        entry_side="Buy",
        entry_order_link_id=link,
    )

    assert result.status is BybitDemoTradeMonitorStatus.AMBIGUOUS
    assert result.reasons == ("EXECUTION_WINDOW_NOT_PROVEN_COMPLETE",)
    assert result.next_entry_allowed is False


def test_position_fill_mismatch_fails_closed() -> None:
    link = "ASTRA-DEMO-E-ABC123"
    client = _FakeReadClient(
        executions=(
            _fill(
                exec_id="entry-1",
                side="Buy",
                qty="1",
                price="100",
                fee="0.06",
                time_ms=1000,
                order_link_id=link,
            ),
            _fill(
                exec_id="exit-1",
                side="Sell",
                qty="0.4",
                price="102",
                fee="0.02448",
                time_ms=2000,
            ),
        ),
        positions=(_position("0.5"),),
    )

    result = reconcile_bybit_demo_trade(
        client=client,
        symbol="BTCUSDT",
        entry_side="Buy",
        entry_order_link_id=link,
    )

    assert result.status is BybitDemoTradeMonitorStatus.AMBIGUOUS
    assert "POSITION_AND_EXECUTION_QUANTITY_MISMATCH" in result.reasons
    assert result.next_entry_allowed is False


def test_monitor_rejects_mainnet_capable_client() -> None:
    client = _FakeReadClient(executions=())
    client.live_mainnet_order_routing_allowed = True
    with pytest.raises(ValueError):
        reconcile_bybit_demo_trade(
            client=client,
            symbol="BTCUSDT",
            entry_side="Buy",
            entry_order_link_id="ASTRA-DEMO-E-ABC123",
        )
