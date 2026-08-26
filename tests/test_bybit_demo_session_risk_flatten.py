from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.execution.bybit_demo import BybitDemoOrderAck
from app.execution.bybit_demo_session_risk_flatten import (
    BybitDemoSessionRiskFlattenPolicy,
    BybitDemoSessionRiskFlattenStatus,
    execute_bybit_demo_session_risk_flatten,
)
from app.marketdata.bybit_demo_quotes import BybitDemoMarketQuote
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.strategy.crypto_perp import CryptoSide
from app.strategy.crypto_session_risk import CryptoSessionRiskState


class _ExcursionStore:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(self, *, initial_quantity: str = "1") -> None:
        self.checkpoint = SimpleNamespace(
            entry_order_link_id="ASTRA-DEMO-E-RISK-FLAT",
            state=SimpleNamespace(
                symbol="BTCUSDT",
                side=CryptoSide.LONG,
                initial_quantity=Decimal(initial_quantity),
            ),
        )

    def load(self):
        return self.checkpoint


class _QuoteClient:
    live_mainnet_order_routing_allowed = False

    def get_quote(self, *, symbol: str) -> BybitDemoMarketQuote:
        assert symbol == "BTCUSDT"
        return BybitDemoMarketQuote(
            symbol="BTCUSDT",
            last_price=Decimal("100"),
            mark_price=Decimal("100"),
            bid_price=Decimal("99.9"),
            ask_price=Decimal("100.1"),
            server_time_ms=1_000,
            received_time_ms=1_000,
            age_ms=0,
        )


class _Client:
    environment = "BYBIT_DEMO"
    live_mainnet_order_routing_allowed = False

    def __init__(
        self,
        positions: list[tuple[SimpleNamespace, ...]],
        *,
        fail_write: bool = False,
    ) -> None:
        self.positions = positions
        self.fail_write = fail_write
        self.requests = []
        self.position_reads = 0

    def get_positions(self, *, settle_coin: str = "USDT"):
        assert settle_coin == "USDT"
        self.position_reads += 1
        if self.positions:
            return self.positions.pop(0)
        return ()

    def place_market_order(self, request):
        self.requests.append(request)
        if self.fail_write:
            raise RuntimeError("ambiguous transport")
        return BybitDemoOrderAck(
            order_id="OID-RISK-FLAT",
            order_link_id=request.order_link_id,
            accepted=True,
        )


def _position(*, size: str = "1") -> SimpleNamespace:
    return SimpleNamespace(
        symbol="BTCUSDT",
        side="Buy",
        size=Decimal(size),
    )


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


def _state(*, current: str) -> CryptoSessionRiskState:
    return CryptoSessionRiskState(
        opening_equity_usdt=Decimal("1000"),
        current_equity_usdt=Decimal(current),
        peak_equity_usdt=Decimal("1100"),
    )


def _enabled() -> BybitDemoSessionRiskFlattenPolicy:
    return BybitDemoSessionRiskFlattenPolicy(
        writes_enabled=True,
        reconciliation_attempts=2,
        reconciliation_delay_seconds=0,
    )


def test_no_drawdown_breach_never_reads_position_or_writes() -> None:
    client = _Client([(_position(),)])
    result = execute_bybit_demo_session_risk_flatten(
        session_state=_state(current="1080"),
        excursion_store=_ExcursionStore(),
        client=client,
        quote_client=_QuoteClient(),
        instrument=_instrument(),
        policy=_enabled(),
    )

    assert result.status is BybitDemoSessionRiskFlattenStatus.NOT_REQUIRED
    assert client.position_reads == 0
    assert client.requests == []


def test_drawdown_breach_is_diagnostic_when_writes_disabled() -> None:
    client = _Client([(_position(),)])
    result = execute_bybit_demo_session_risk_flatten(
        session_state=_state(current="1040"),
        excursion_store=_ExcursionStore(),
        client=client,
        quote_client=_QuoteClient(),
        instrument=_instrument(),
    )

    assert result.status is BybitDemoSessionRiskFlattenStatus.WRITES_DISABLED
    assert "SESSION_DRAWDOWN_LIMIT_BREACHED" in result.reasons
    assert client.position_reads == 0
    assert client.requests == []


def test_breach_closes_exact_current_residual_reduce_only_and_reconciles() -> None:
    client = _Client([(_position(size="0.4"),), ()])
    result = execute_bybit_demo_session_risk_flatten(
        session_state=_state(current="1040"),
        excursion_store=_ExcursionStore(initial_quantity="1"),
        client=client,
        quote_client=_QuoteClient(),
        instrument=_instrument(),
        policy=_enabled(),
    )

    assert result.status is BybitDemoSessionRiskFlattenStatus.CLOSE_CONFIRMED
    assert result.position_closed is True
    assert len(client.requests) == 1
    request = client.requests[0]
    assert request.symbol == "BTCUSDT"
    assert request.side == "Sell"
    assert request.quantity == Decimal("0.4")
    assert request.reduce_only is True
    assert request.order_link_id.startswith("ASTRA-DEMO-R-")


def test_increased_exposure_is_blocked_before_order_write() -> None:
    client = _Client([(_position(size="1.1"),)])
    result = execute_bybit_demo_session_risk_flatten(
        session_state=_state(current="1040"),
        excursion_store=_ExcursionStore(initial_quantity="1"),
        client=client,
        quote_client=_QuoteClient(),
        instrument=_instrument(),
        policy=_enabled(),
    )

    assert result.status is BybitDemoSessionRiskFlattenStatus.CLOSE_BLOCKED
    assert result.reasons == ("SESSION_RISK_POSITION_EXCEEDS_DURABLE_BASELINE",)
    assert client.requests == []


def test_ambiguous_close_write_is_never_retried() -> None:
    client = _Client([(_position(),)], fail_write=True)
    result = execute_bybit_demo_session_risk_flatten(
        session_state=_state(current="1040"),
        excursion_store=_ExcursionStore(),
        client=client,
        quote_client=_QuoteClient(),
        instrument=_instrument(),
        policy=_enabled(),
    )

    assert result.status is BybitDemoSessionRiskFlattenStatus.CLOSE_WRITE_FAILED
    assert len(client.requests) == 1
    assert client.position_reads == 1


def test_ack_without_broker_close_proof_remains_unresolved() -> None:
    client = _Client([(_position(),), (_position(),), (_position(),)])
    result = execute_bybit_demo_session_risk_flatten(
        session_state=_state(current="1040"),
        excursion_store=_ExcursionStore(),
        client=client,
        quote_client=_QuoteClient(),
        instrument=_instrument(),
        policy=_enabled(),
    )

    assert result.status is BybitDemoSessionRiskFlattenStatus.CLOSE_UNRESOLVED
    assert result.position_closed is False
    assert result.residual_size == Decimal("1")
    assert len(client.requests) == 1


def test_mainnet_capable_client_is_hard_rejected() -> None:
    client = _Client([(_position(),)])
    client.live_mainnet_order_routing_allowed = True

    with pytest.raises(ValueError, match="mainnet-capable order client"):
        execute_bybit_demo_session_risk_flatten(
            session_state=_state(current="1040"),
            excursion_store=_ExcursionStore(),
            client=client,
            quote_client=_QuoteClient(),
            instrument=_instrument(),
            policy=_enabled(),
        )
