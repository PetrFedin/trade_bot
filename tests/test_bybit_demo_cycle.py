from decimal import Decimal

from app.execution.bybit_demo import (
    BybitDemoOrderAck,
    BybitDemoPosition,
    BybitDemoProtectionAck,
)
from app.execution.bybit_demo_cycle import (
    BybitDemoCyclePolicy,
    BybitDemoCycleStatus,
    execute_bybit_demo_trade_cycle,
)
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.strategy.crypto_perp import CryptoPerpStrategyConfig, CryptoSide, CryptoTradePlan
from app.strategy.crypto_session_risk import CryptoSessionRiskState


class _FakeDemoClient:
    live_mainnet_order_routing_allowed = False

    def __init__(
        self,
        position_snapshots: list[tuple[BybitDemoPosition, ...]],
        *,
        protection_error: Exception | None = None,
        flatten_error: Exception | None = None,
    ) -> None:
        self.position_snapshots = position_snapshots
        self.protection_error = protection_error
        self.flatten_error = flatten_error
        self.orders: list[object] = []
        self.protections: list[object] = []
        self.position_reads = 0

    def get_positions(self, *, settle_coin: str = "USDT") -> tuple[BybitDemoPosition, ...]:
        assert settle_coin == "USDT"
        index = min(self.position_reads, len(self.position_snapshots) - 1)
        self.position_reads += 1
        return self.position_snapshots[index]

    def place_market_order(self, request: object) -> BybitDemoOrderAck:
        reduce_only = bool(getattr(request, "reduce_only"))
        if reduce_only and self.flatten_error is not None:
            raise self.flatten_error
        self.orders.append(request)
        order_link_id = str(getattr(request, "order_link_id"))
        return BybitDemoOrderAck(
            order_id=f"order-{len(self.orders)}",
            order_link_id=order_link_id,
            accepted=True,
        )

    def set_full_position_protection(self, request: object) -> BybitDemoProtectionAck:
        if self.protection_error is not None:
            raise self.protection_error
        self.protections.append(request)
        return BybitDemoProtectionAck(
            symbol=str(getattr(request, "symbol")),
            take_profit_price=getattr(request, "take_profit_price"),
            stop_loss_price=getattr(request, "stop_loss_price"),
        )


def _instrument() -> BybitInstrumentSpec:
    return BybitInstrumentSpec(
        symbol="BTCUSDT",
        status="Trading",
        contract_type="LinearPerpetual",
        base_coin="BTC",
        quote_coin="USDT",
        settle_coin="USDT",
        tick_size=Decimal("0.10"),
        min_order_qty=Decimal("0.001"),
        qty_step=Decimal("0.001"),
        min_notional_value=Decimal("5"),
        max_market_order_qty=Decimal("500"),
        max_leverage=Decimal("100"),
        funding_interval_minutes=480,
    )


def _trade_plan() -> CryptoTradePlan:
    return CryptoTradePlan(
        symbol="BTCUSDT",
        side=CryptoSide.LONG,
        decision_time="2026-08-12T18:00:00+00:00",
        reference_price=Decimal("100000"),
        notional_usdt=Decimal("1200"),
        reference_quantity=Decimal("0.01249"),
        risk_budget_usdt=Decimal("10"),
        stop_fraction=Decimal("0.004"),
        estimated_round_trip_cost_usdt=Decimal("1.92"),
        estimated_stop_loss_after_cost_usdt=Decimal("6.72"),
        target_net_profit_usd=Decimal("15"),
        required_move_fraction=Decimal("0.0141"),
        expected_move_fraction=Decimal("0.015"),
        expected_net_edge_usd=Decimal("16.08"),
        quality_score=Decimal("2.5"),
    )


def _session() -> CryptoSessionRiskState:
    return CryptoSessionRiskState(
        opening_equity_usdt=Decimal("1000"),
        current_equity_usdt=Decimal("1005"),
        peak_equity_usdt=Decimal("1010"),
        realized_pnl_usdt=Decimal("5"),
        execution_cost_usdt=Decimal("4"),
        consecutive_losses=0,
    )


def _position(quantity: str = "0.012") -> BybitDemoPosition:
    return BybitDemoPosition(
        symbol="BTCUSDT",
        side="Buy",
        size=Decimal(quantity),
        average_price=Decimal("100050"),
        unrealised_pnl=Decimal("0"),
    )


def _enabled_policy() -> BybitDemoCyclePolicy:
    return BybitDemoCyclePolicy(
        writes_enabled=True,
        reconciliation_attempts=2,
        reconciliation_delay_seconds=0,
    )


def test_demo_cycle_is_non_writing_by_default() -> None:
    client = _FakeDemoClient([()])

    result = execute_bybit_demo_trade_cycle(
        _trade_plan(),
        instrument=_instrument(),
        strategy_config=CryptoPerpStrategyConfig(),
        session_state=_session(),
        client=client,
    )

    assert result.status is BybitDemoCycleStatus.DEMO_WRITES_DISABLED
    assert result.demo_order_writes_enabled is False
    assert result.live_mainnet_order_routing_allowed is False
    assert client.orders == []
    assert client.position_reads == 0


def test_demo_cycle_reconciles_fill_before_exchange_protection() -> None:
    client = _FakeDemoClient([(), (_position(),)])

    result = execute_bybit_demo_trade_cycle(
        _trade_plan(),
        instrument=_instrument(),
        strategy_config=CryptoPerpStrategyConfig(),
        session_state=_session(),
        client=client,
        cycle_policy=_enabled_policy(),
        sleeper=lambda _seconds: None,
    )

    assert result.status is BybitDemoCycleStatus.PROTECTED
    assert result.entry_ack is not None
    assert result.protection_ack is not None
    assert result.flatten_ack is None
    assert result.reconciled_position == _position()
    assert result.next_entry_allowed is True
    assert len(client.orders) == 1
    assert getattr(client.orders[0], "reduce_only") is False
    assert len(client.protections) == 1


def test_preexisting_symbol_position_blocks_new_entry() -> None:
    client = _FakeDemoClient([(_position(),)])

    result = execute_bybit_demo_trade_cycle(
        _trade_plan(),
        instrument=_instrument(),
        strategy_config=CryptoPerpStrategyConfig(),
        session_state=_session(),
        client=client,
        cycle_policy=_enabled_policy(),
    )

    assert result.status is BybitDemoCycleStatus.PREEXISTING_POSITION_BLOCKED
    assert result.next_entry_allowed is False
    assert client.orders == []


def test_order_ack_without_reconciled_fill_never_counts_as_protected() -> None:
    client = _FakeDemoClient([(), (), ()])

    result = execute_bybit_demo_trade_cycle(
        _trade_plan(),
        instrument=_instrument(),
        strategy_config=CryptoPerpStrategyConfig(),
        session_state=_session(),
        client=client,
        cycle_policy=_enabled_policy(),
        sleeper=lambda _seconds: None,
    )

    assert result.status is BybitDemoCycleStatus.ENTRY_ACKED_FILL_UNRESOLVED
    assert result.entry_ack is not None
    assert result.protection_ack is None
    assert result.next_entry_allowed is False
    assert len(client.orders) == 1


def test_post_fill_risk_breach_sets_protection_then_reduce_only_flatten() -> None:
    client = _FakeDemoClient([(), (_position("0.020"),)])

    result = execute_bybit_demo_trade_cycle(
        _trade_plan(),
        instrument=_instrument(),
        strategy_config=CryptoPerpStrategyConfig(),
        session_state=_session(),
        client=client,
        cycle_policy=_enabled_policy(),
        sleeper=lambda _seconds: None,
    )

    assert result.status is BybitDemoCycleStatus.PROTECTED_THEN_FLATTEN_REQUESTED
    assert result.protection_ack is not None
    assert result.flatten_ack is not None
    assert "POST_FILL_RISK_BUDGET_EXCEEDED" in result.reasons
    assert len(client.protections) == 1
    assert len(client.orders) == 2
    assert getattr(client.orders[1], "reduce_only") is True
    assert result.next_entry_allowed is False


def test_exchange_protection_failure_attempts_reduce_only_flatten() -> None:
    client = _FakeDemoClient(
        [(), (_position(),)],
        protection_error=RuntimeError("simulated-protection-failure"),
    )

    result = execute_bybit_demo_trade_cycle(
        _trade_plan(),
        instrument=_instrument(),
        strategy_config=CryptoPerpStrategyConfig(),
        session_state=_session(),
        client=client,
        cycle_policy=_enabled_policy(),
        sleeper=lambda _seconds: None,
    )

    assert result.status is BybitDemoCycleStatus.PROTECTION_FAILED_FLATTEN_REQUESTED
    assert result.flatten_ack is not None
    assert result.protection_ack is None
    assert result.reasons == ("EXCHANGE_PROTECTION_WRITE_FAILED:RuntimeError",)
    assert getattr(client.orders[-1], "reduce_only") is True
    assert result.next_entry_allowed is False
