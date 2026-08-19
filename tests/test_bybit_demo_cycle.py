from decimal import Decimal
from typing import Any

from app.execution.bybit_demo import (
    BybitDemoFeeRate,
    BybitDemoOrderAck,
    BybitDemoPosition,
    BybitDemoProtectionAck,
    BybitDemoRunnerProtectionAck,
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
        taker_fee_rate: Decimal = Decimal("0.0006"),
        maker_fee_rate: Decimal = Decimal("0.0001"),
        fee_error: Exception | None = None,
        protection_error: Exception | None = None,
        flatten_error: Exception | None = None,
    ) -> None:
        self.position_snapshots = position_snapshots
        self.taker_fee_rate = taker_fee_rate
        self.maker_fee_rate = maker_fee_rate
        self.fee_error = fee_error
        self.protection_error = protection_error
        self.flatten_error = flatten_error
        self.orders: list[Any] = []
        self.protections: list[Any] = []
        self.position_reads = 0
        self.fee_reads = 0

    def get_fee_rate(self, *, symbol: str) -> BybitDemoFeeRate:
        self.fee_reads += 1
        if self.fee_error is not None:
            raise self.fee_error
        return BybitDemoFeeRate(
            symbol=symbol,
            taker_fee_rate=self.taker_fee_rate,
            maker_fee_rate=self.maker_fee_rate,
        )

    def get_positions(self, *, settle_coin: str = "USDT") -> tuple[BybitDemoPosition, ...]:
        assert settle_coin == "USDT"
        index = min(self.position_reads, len(self.position_snapshots) - 1)
        self.position_reads += 1
        return self.position_snapshots[index]

    def place_market_order(self, request: Any) -> BybitDemoOrderAck:
        reduce_only = bool(request.reduce_only)
        if reduce_only and self.flatten_error is not None:
            raise self.flatten_error
        self.orders.append(request)
        order_link_id = str(request.order_link_id)
        return BybitDemoOrderAck(
            order_id=f"order-{len(self.orders)}",
            order_link_id=order_link_id,
            accepted=True,
        )

    def set_full_position_protection(self, request: Any) -> BybitDemoProtectionAck:
        if self.protection_error is not None:
            raise self.protection_error
        self.protections.append(request)
        return BybitDemoProtectionAck(
            symbol=str(request.symbol),
            take_profit_price=request.take_profit_price,
            stop_loss_price=request.stop_loss_price,
        )

    def set_open_ended_position_protection(
        self,
        request: Any,
    ) -> BybitDemoRunnerProtectionAck:
        if self.protection_error is not None:
            raise self.protection_error
        self.protections.append(request)
        return BybitDemoRunnerProtectionAck(
            symbol=str(request.symbol),
            stop_loss_price=request.stop_loss_price,
            trailing_stop_distance=request.trailing_stop_distance,
            trailing_active_price=request.trailing_active_price,
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


def _trade_plan(
    target: Decimal = Decimal("20"),
    *,
    expected_move_fraction: Decimal = Decimal("0.027"),
    expected_net_edge_usd: Decimal = Decimal("31"),
) -> CryptoTradePlan:
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
        target_net_profit_usd=target,
        required_move_fraction=Decimal("0.018"),
        expected_move_fraction=expected_move_fraction,
        expected_net_edge_usd=expected_net_edge_usd,
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


def _position(
    quantity: str = "0.012",
    *,
    liquidation_price: Decimal | None = Decimal("99000"),
) -> BybitDemoPosition:
    return BybitDemoPosition(
        symbol="BTCUSDT",
        side="Buy",
        size=Decimal(quantity),
        average_price=Decimal("100050"),
        unrealised_pnl=Decimal("0"),
        liquidation_price=liquidation_price,
    )


def _enabled_policy() -> BybitDemoCyclePolicy:
    return BybitDemoCyclePolicy(
        writes_enabled=True,
        reconciliation_attempts=2,
        reconciliation_delay_seconds=0,
    )


def _strategy_config() -> CryptoPerpStrategyConfig:
    return CryptoPerpStrategyConfig(target_net_profit_usd=Decimal("20"))


def test_demo_cycle_is_non_writing_by_default() -> None:
    client = _FakeDemoClient([()])
    result = execute_bybit_demo_trade_cycle(
        _trade_plan(),
        instrument=_instrument(),
        strategy_config=_strategy_config(),
        session_state=_session(),
        client=client,
    )
    assert result.status is BybitDemoCycleStatus.DEMO_WRITES_DISABLED
    assert result.demo_order_writes_enabled is False
    assert result.live_mainnet_order_routing_allowed is False
    assert client.orders == []
    assert client.position_reads == 0
    assert client.fee_reads == 0


def test_demo_cycle_blocks_15_dollar_entry_instead_of_falling_back() -> None:
    client = _FakeDemoClient([()])
    result = execute_bybit_demo_trade_cycle(
        _trade_plan(Decimal("15")),
        instrument=_instrument(),
        strategy_config=CryptoPerpStrategyConfig(),
        session_state=_session(),
        client=client,
        cycle_policy=_enabled_policy(),
    )
    assert result.status is BybitDemoCycleStatus.ENTRY_BLOCKED
    assert result.reasons == ("CRYPTO_ENTRY_MINIMUM_20_USD_NET_EDGE_REQUIRED",)
    assert client.orders == []
    assert client.fee_reads == 0


def test_account_fee_reconciliation_blocks_edge_that_no_longer_supports_20_net() -> None:
    client = _FakeDemoClient([()], taker_fee_rate=Decimal("0.0055"))
    result = execute_bybit_demo_trade_cycle(
        _trade_plan(),
        instrument=_instrument(),
        strategy_config=_strategy_config(),
        session_state=_session(),
        client=client,
        cycle_policy=_enabled_policy(),
    )

    assert result.status is BybitDemoCycleStatus.ENTRY_BLOCKED
    assert "ACCOUNT_FEE_EXPECTED_NET_PROFIT_BELOW_TARGET" in result.reasons
    assert result.account_taker_fee_rate == Decimal("0.0055")
    assert client.fee_reads == 1
    assert client.orders == []


def test_unresolved_account_fee_rate_blocks_before_order_write() -> None:
    client = _FakeDemoClient([()], fee_error=RuntimeError("fee-unavailable"))
    result = execute_bybit_demo_trade_cycle(
        _trade_plan(),
        instrument=_instrument(),
        strategy_config=_strategy_config(),
        session_state=_session(),
        client=client,
        cycle_policy=_enabled_policy(),
    )

    assert result.status is BybitDemoCycleStatus.ENTRY_BLOCKED
    assert result.reasons == ("ACCOUNT_FEE_RATE_RECONCILIATION_FAILED:RuntimeError",)
    assert client.orders == []


def test_thin_excess_edge_keeps_cost_aware_fixed_20_target() -> None:
    client = _FakeDemoClient([(), (_position(),)])
    result = execute_bybit_demo_trade_cycle(
        _trade_plan(
            expected_move_fraction=Decimal("0.020"),
            expected_net_edge_usd=Decimal("24"),
        ),
        instrument=_instrument(),
        strategy_config=_strategy_config(),
        session_state=_session(),
        client=client,
        cycle_policy=_enabled_policy(),
        sleeper=lambda _seconds: None,
    )

    assert result.status is BybitDemoCycleStatus.PROTECTED
    assert result.exit_mode == "FIXED_20_TARGET"
    assert "RUNNER_EXCESS_EXPECTED_EDGE_TOO_THIN" in result.runner_admission_reasons
    assert result.liquidation_safety_reason == "SAFE"
    assert result.stop_to_liquidation_r is not None
    assert result.stop_to_liquidation_r >= Decimal("1")
    assert len(client.protections) == 1
    fixed_request = client.protections[0]
    assert fixed_request.take_profit_price > Decimal("100050")
    assert fixed_request.stop_loss_price < Decimal("100050")
    assert not hasattr(fixed_request, "trailing_stop_distance")
    assert result.live_mainnet_order_routing_allowed is False


def test_demo_cycle_reconciles_fill_before_uncapped_runner_protection() -> None:
    client = _FakeDemoClient([(), (_position(),)])
    result = execute_bybit_demo_trade_cycle(
        _trade_plan(),
        instrument=_instrument(),
        strategy_config=_strategy_config(),
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
    assert result.account_taker_fee_rate == Decimal("0.0006")
    assert result.account_maker_fee_rate == Decimal("0.0001")
    assert result.exit_mode == "OPEN_ENDED_RUNNER"
    assert result.runner_admission_reasons == ()
    assert result.liquidation_safety_reason == "SAFE"
    assert result.stop_to_liquidation_r is not None
    assert len(client.orders) == 1
    assert client.orders[0].reduce_only is False
    assert len(client.protections) == 1
    runner_request = client.protections[0]
    assert not hasattr(runner_request, "take_profit_price")
    assert runner_request.trailing_stop_distance > 0
    assert runner_request.trailing_active_price > Decimal("100050")


def test_missing_liquidation_price_protects_then_flattens_reduce_only() -> None:
    client = _FakeDemoClient([(), (_position(liquidation_price=None),)])
    result = execute_bybit_demo_trade_cycle(
        _trade_plan(),
        instrument=_instrument(),
        strategy_config=_strategy_config(),
        session_state=_session(),
        client=client,
        cycle_policy=_enabled_policy(),
        sleeper=lambda _seconds: None,
    )

    assert result.status is BybitDemoCycleStatus.PROTECTED_THEN_FLATTEN_REQUESTED
    assert result.protection_ack is not None
    assert result.flatten_ack is not None
    assert result.reasons == ("LIQUIDATION_PRICE_UNAVAILABLE",)
    assert result.liquidation_safety_reason == "LIQUIDATION_PRICE_UNAVAILABLE"
    assert result.stop_to_liquidation_r is None
    assert len(client.protections) == 1
    assert len(client.orders) == 2
    assert client.orders[0].reduce_only is False
    assert client.orders[1].reduce_only is True
    assert result.next_entry_allowed is False


def test_liquidation_inside_hard_stop_protects_then_flattens_reduce_only() -> None:
    client = _FakeDemoClient(
        [(), (_position(liquidation_price=Decimal("100000")),)]
    )
    result = execute_bybit_demo_trade_cycle(
        _trade_plan(),
        instrument=_instrument(),
        strategy_config=_strategy_config(),
        session_state=_session(),
        client=client,
        cycle_policy=_enabled_policy(),
        sleeper=lambda _seconds: None,
    )

    assert result.status is BybitDemoCycleStatus.PROTECTED_THEN_FLATTEN_REQUESTED
    assert result.reasons == ("LIQUIDATION_NOT_BEYOND_HARD_STOP",)
    assert result.liquidation_safety_reason == "LIQUIDATION_NOT_BEYOND_HARD_STOP"
    assert result.stop_to_liquidation_r is None
    assert result.protection_ack is not None
    assert result.flatten_ack is not None
    assert client.orders[-1].reduce_only is True


def test_preexisting_symbol_position_blocks_new_entry() -> None:
    client = _FakeDemoClient([(_position(),)])
    result = execute_bybit_demo_trade_cycle(
        _trade_plan(),
        instrument=_instrument(),
        strategy_config=_strategy_config(),
        session_state=_session(),
        client=client,
        cycle_policy=_enabled_policy(),
    )
    assert result.status is BybitDemoCycleStatus.PREEXISTING_POSITION_BLOCKED
    assert result.next_entry_allowed is False
    assert client.orders == []
    assert client.fee_reads == 0


def test_order_ack_without_reconciled_fill_never_counts_as_protected() -> None:
    client = _FakeDemoClient([(), (), ()])
    result = execute_bybit_demo_trade_cycle(
        _trade_plan(),
        instrument=_instrument(),
        strategy_config=_strategy_config(),
        session_state=_session(),
        client=client,
        cycle_policy=_enabled_policy(),
        sleeper=lambda _seconds: None,
    )
    assert result.status is BybitDemoCycleStatus.ENTRY_ACKED_FILL_UNRESOLVED
    assert result.entry_ack is not None
    assert result.protection_ack is None
    assert result.next_entry_allowed is False
    assert result.exit_mode == "OPEN_ENDED_RUNNER"
    assert len(client.orders) == 1


def test_post_fill_risk_breach_sets_protection_then_reduce_only_flatten() -> None:
    client = _FakeDemoClient([(), (_position("0.020"),)])
    result = execute_bybit_demo_trade_cycle(
        _trade_plan(),
        instrument=_instrument(),
        strategy_config=_strategy_config(),
        session_state=_session(),
        client=client,
        cycle_policy=_enabled_policy(),
        sleeper=lambda _seconds: None,
    )
    assert result.status is BybitDemoCycleStatus.PROTECTED_THEN_FLATTEN_REQUESTED
    assert result.protection_ack is not None
    assert result.flatten_ack is not None
    assert result.exit_mode == "FIXED_20_TARGET"
    assert "POST_FILL_RISK_BUDGET_EXCEEDED" in result.reasons
    assert len(client.protections) == 1
    fixed_request = client.protections[0]
    assert hasattr(fixed_request, "take_profit_price")
    assert len(client.orders) == 2
    assert client.orders[1].reduce_only is True
    assert result.next_entry_allowed is False


def test_exchange_protection_failure_attempts_reduce_only_flatten() -> None:
    client = _FakeDemoClient(
        [(), (_position(),)],
        protection_error=RuntimeError("simulated-protection-failure"),
    )
    result = execute_bybit_demo_trade_cycle(
        _trade_plan(),
        instrument=_instrument(),
        strategy_config=_strategy_config(),
        session_state=_session(),
        client=client,
        cycle_policy=_enabled_policy(),
        sleeper=lambda _seconds: None,
    )
    assert result.status is BybitDemoCycleStatus.PROTECTION_FAILED_FLATTEN_REQUESTED
    assert result.flatten_ack is not None
    assert result.protection_ack is None
    assert result.reasons == ("EXCHANGE_PROTECTION_WRITE_FAILED:RuntimeError",)
    assert client.orders[-1].reduce_only is True
    assert result.next_entry_allowed is False
