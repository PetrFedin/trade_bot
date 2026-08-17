from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from app.execution.bybit_demo import (
    BybitDemoFeeRate,
    BybitDemoOrderAck,
    BybitDemoOrderClient,
    BybitDemoPosition,
    BybitDemoRunnerProtectionAck,
)
from app.execution.bybit_demo_controller import (
    plan_bybit_demo_entry,
    plan_bybit_demo_reduce_only_close,
    plan_bybit_demo_runner_protection_after_fill,
)
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.strategy.crypto_entry_economics import revalidate_entry_at_actual_taker_fee
from app.strategy.crypto_perp import CryptoPerpStrategyConfig, CryptoSide, CryptoTradePlan
from app.strategy.crypto_session_risk import CryptoSessionRiskPolicy, CryptoSessionRiskState


class BybitDemoCycleStatus(StrEnum):
    DEMO_WRITES_DISABLED = "DEMO_WRITES_DISABLED"
    PREEXISTING_POSITION_BLOCKED = "PREEXISTING_POSITION_BLOCKED"
    ENTRY_BLOCKED = "ENTRY_BLOCKED"
    ENTRY_ACKED_FILL_UNRESOLVED = "ENTRY_ACKED_FILL_UNRESOLVED"
    PROTECTED = "PROTECTED"
    PROTECTED_THEN_FLATTEN_REQUESTED = "PROTECTED_THEN_FLATTEN_REQUESTED"
    PROTECTION_FAILED_FLATTEN_REQUESTED = "PROTECTION_FAILED_FLATTEN_REQUESTED"
    FLATTEN_REQUEST_FAILED = "FLATTEN_REQUEST_FAILED"


@dataclass(frozen=True)
class BybitDemoCyclePolicy:
    writes_enabled: bool = False
    reconciliation_attempts: int = 4
    reconciliation_delay_seconds: float = 0.25

    def validate(self) -> None:
        if self.reconciliation_attempts < 1:
            raise ValueError("Bybit demo reconciliation attempts must be positive")
        if self.reconciliation_delay_seconds < 0:
            raise ValueError("Bybit demo reconciliation delay cannot be negative")


@dataclass(frozen=True)
class BybitDemoCycleResult:
    status: BybitDemoCycleStatus
    reasons: tuple[str, ...]
    entry_ack: BybitDemoOrderAck | None
    protection_ack: BybitDemoRunnerProtectionAck | None
    flatten_ack: BybitDemoOrderAck | None
    reconciled_position: BybitDemoPosition | None
    next_entry_allowed: bool
    demo_order_writes_enabled: bool
    account_taker_fee_rate: Decimal | None = None
    account_maker_fee_rate: Decimal | None = None
    live_mainnet_order_routing_allowed: bool = False


class _DemoClient(Protocol):
    @property
    def live_mainnet_order_routing_allowed(self) -> bool: ...

    def get_fee_rate(self, *, symbol: str) -> BybitDemoFeeRate: ...

    def get_positions(self, *, settle_coin: str = "USDT") -> tuple[BybitDemoPosition, ...]: ...

    def place_market_order(self, request: object) -> BybitDemoOrderAck: ...

    def set_open_ended_position_protection(
        self,
        request: object,
    ) -> BybitDemoRunnerProtectionAck: ...


Sleeper = Callable[[float], None]


def execute_bybit_demo_trade_cycle(
    trade_plan: CryptoTradePlan,
    *,
    instrument: BybitInstrumentSpec,
    strategy_config: CryptoPerpStrategyConfig,
    session_state: CryptoSessionRiskState,
    client: BybitDemoOrderClient | _DemoClient,
    cycle_policy: BybitDemoCyclePolicy | None = None,
    session_policy: CryptoSessionRiskPolicy | None = None,
    sleeper: Sleeper = time.sleep,
) -> BybitDemoCycleResult:
    """Execute one demo-only entry -> reconcile -> uncapped runner protection cycle.

    This function has no mainnet path. Writes are disabled unless an explicit cycle policy
    enables them. A position is never called protected merely because order creation ACKed.
    Before an order is submitted, the account-specific taker fee and instrument-normalized
    quantity must still support the planned net target and risk budget. Successful protection
    deliberately omits a fixed take-profit ceiling.
    """

    policy = BybitDemoCyclePolicy() if cycle_policy is None else cycle_policy
    policy.validate()
    if client.live_mainnet_order_routing_allowed:
        raise ValueError("Bybit demo cycle rejected a client that permits mainnet routing")
    if not policy.writes_enabled:
        return _result(
            BybitDemoCycleStatus.DEMO_WRITES_DISABLED,
            reasons=("EXPLICIT_DEMO_WRITE_ENABLE_REQUIRED",),
            writes_enabled=False,
        )

    preexisting = _matching_open_position(client.get_positions(), trade_plan)
    if preexisting is not None:
        return _result(
            BybitDemoCycleStatus.PREEXISTING_POSITION_BLOCKED,
            reasons=("PREEXISTING_SYMBOL_POSITION_REQUIRES_RECONCILIATION",),
            position=preexisting,
            writes_enabled=True,
        )

    entry_plan = plan_bybit_demo_entry(
        trade_plan,
        instrument=instrument,
        session_state=session_state,
        session_policy=session_policy,
    )
    if not entry_plan.eligible or entry_plan.order is None:
        return _result(
            BybitDemoCycleStatus.ENTRY_BLOCKED,
            reasons=entry_plan.reasons,
            writes_enabled=True,
        )

    try:
        fee_rate = client.get_fee_rate(symbol=trade_plan.symbol)
    except Exception as exc:  # noqa: BLE001 - unresolved fees must block before any order write.
        return _result(
            BybitDemoCycleStatus.ENTRY_BLOCKED,
            reasons=(f"ACCOUNT_FEE_RATE_RECONCILIATION_FAILED:{type(exc).__name__}",),
            writes_enabled=True,
        )

    effective_strategy_config = replace(
        strategy_config,
        taker_fee_rate=fee_rate.taker_fee_rate,
    )
    execution_notional = entry_plan.order.quantity * trade_plan.reference_price
    fee_decision = revalidate_entry_at_actual_taker_fee(
        trade_plan,
        execution_notional_usdt=execution_notional,
        actual_taker_fee_rate=fee_rate.taker_fee_rate,
        strategy_config=effective_strategy_config,
    )
    if not fee_decision.eligible:
        return _result(
            BybitDemoCycleStatus.ENTRY_BLOCKED,
            reasons=fee_decision.reasons,
            fee_rate=fee_rate,
            writes_enabled=True,
        )

    entry_ack = client.place_market_order(entry_plan.order)
    position = _reconcile_position(
        client,
        trade_plan=trade_plan,
        attempts=policy.reconciliation_attempts,
        delay_seconds=policy.reconciliation_delay_seconds,
        sleeper=sleeper,
    )
    if position is None or position.average_price is None:
        return _result(
            BybitDemoCycleStatus.ENTRY_ACKED_FILL_UNRESOLVED,
            reasons=("ORDER_ACK_IS_NOT_FILL_CONFIRMATION",),
            entry_ack=entry_ack,
            fee_rate=fee_rate,
            writes_enabled=True,
        )

    protection_plan = plan_bybit_demo_runner_protection_after_fill(
        trade_plan,
        actual_average_entry_price=position.average_price,
        actual_filled_quantity=position.size,
        instrument=instrument,
        strategy_config=effective_strategy_config,
    )
    if protection_plan.protection is None:
        return _flatten_after_protection_failure(
            trade_plan,
            instrument=instrument,
            client=client,
            position=position,
            entry_ack=entry_ack,
            fee_rate=fee_rate,
            reasons=protection_plan.reasons,
        )

    try:
        protection_ack = client.set_open_ended_position_protection(protection_plan.protection)
    except Exception as exc:  # noqa: BLE001 - protection failure must trigger a close attempt.
        return _flatten_after_protection_failure(
            trade_plan,
            instrument=instrument,
            client=client,
            position=position,
            entry_ack=entry_ack,
            fee_rate=fee_rate,
            reasons=(f"EXCHANGE_PROTECTION_WRITE_FAILED:{type(exc).__name__}",),
        )

    if protection_plan.flatten_required:
        return _flatten_protected_position(
            trade_plan,
            instrument=instrument,
            client=client,
            position=position,
            entry_ack=entry_ack,
            protection_ack=protection_ack,
            fee_rate=fee_rate,
            reasons=protection_plan.reasons,
        )

    return _result(
        BybitDemoCycleStatus.PROTECTED,
        reasons=(),
        entry_ack=entry_ack,
        protection_ack=protection_ack,
        position=position,
        next_entry_allowed=True,
        fee_rate=fee_rate,
        writes_enabled=True,
    )


def _reconcile_position(
    client: _DemoClient,
    *,
    trade_plan: CryptoTradePlan,
    attempts: int,
    delay_seconds: float,
    sleeper: Sleeper,
) -> BybitDemoPosition | None:
    for attempt in range(attempts):
        position = _matching_open_position(client.get_positions(), trade_plan)
        if position is not None and position.average_price is not None:
            return position
        if attempt + 1 < attempts and delay_seconds > 0:
            sleeper(delay_seconds)
    return None


def _matching_open_position(
    positions: tuple[BybitDemoPosition, ...],
    trade_plan: CryptoTradePlan,
) -> BybitDemoPosition | None:
    expected_side = "Buy" if trade_plan.side is CryptoSide.LONG else "Sell"
    for position in positions:
        if position.symbol != trade_plan.symbol or position.size <= 0:
            continue
        if position.side != expected_side:
            return position
        return position
    return None


def _flatten_after_protection_failure(
    trade_plan: CryptoTradePlan,
    *,
    instrument: BybitInstrumentSpec,
    client: _DemoClient,
    position: BybitDemoPosition,
    entry_ack: BybitDemoOrderAck,
    fee_rate: BybitDemoFeeRate,
    reasons: tuple[str, ...],
) -> BybitDemoCycleResult:
    try:
        close = plan_bybit_demo_reduce_only_close(
            trade_plan,
            open_quantity=position.size,
            instrument=instrument,
        )
        flatten_ack = client.place_market_order(close)
    except Exception as exc:  # noqa: BLE001 - result must explicitly expose failed emergency close.
        return _result(
            BybitDemoCycleStatus.FLATTEN_REQUEST_FAILED,
            reasons=(*reasons, f"EMERGENCY_REDUCE_ONLY_CLOSE_FAILED:{type(exc).__name__}"),
            entry_ack=entry_ack,
            position=position,
            fee_rate=fee_rate,
            writes_enabled=True,
        )
    return _result(
        BybitDemoCycleStatus.PROTECTION_FAILED_FLATTEN_REQUESTED,
        reasons=reasons,
        entry_ack=entry_ack,
        flatten_ack=flatten_ack,
        position=position,
        fee_rate=fee_rate,
        writes_enabled=True,
    )


def _flatten_protected_position(
    trade_plan: CryptoTradePlan,
    *,
    instrument: BybitInstrumentSpec,
    client: _DemoClient,
    position: BybitDemoPosition,
    entry_ack: BybitDemoOrderAck,
    protection_ack: BybitDemoRunnerProtectionAck,
    fee_rate: BybitDemoFeeRate,
    reasons: tuple[str, ...],
) -> BybitDemoCycleResult:
    try:
        close = plan_bybit_demo_reduce_only_close(
            trade_plan,
            open_quantity=position.size,
            instrument=instrument,
        )
        flatten_ack = client.place_market_order(close)
    except Exception as exc:  # noqa: BLE001 - result must explicitly expose failed emergency close.
        return _result(
            BybitDemoCycleStatus.FLATTEN_REQUEST_FAILED,
            reasons=(*reasons, f"EMERGENCY_REDUCE_ONLY_CLOSE_FAILED:{type(exc).__name__}"),
            entry_ack=entry_ack,
            protection_ack=protection_ack,
            position=position,
            fee_rate=fee_rate,
            writes_enabled=True,
        )
    return _result(
        BybitDemoCycleStatus.PROTECTED_THEN_FLATTEN_REQUESTED,
        reasons=reasons,
        entry_ack=entry_ack,
        protection_ack=protection_ack,
        flatten_ack=flatten_ack,
        position=position,
        fee_rate=fee_rate,
        writes_enabled=True,
    )


def _result(
    status: BybitDemoCycleStatus,
    *,
    reasons: tuple[str, ...],
    entry_ack: BybitDemoOrderAck | None = None,
    protection_ack: BybitDemoRunnerProtectionAck | None = None,
    flatten_ack: BybitDemoOrderAck | None = None,
    position: BybitDemoPosition | None = None,
    next_entry_allowed: bool = False,
    fee_rate: BybitDemoFeeRate | None = None,
    writes_enabled: bool,
) -> BybitDemoCycleResult:
    return BybitDemoCycleResult(
        status=status,
        reasons=reasons,
        entry_ack=entry_ack,
        protection_ack=protection_ack,
        flatten_ack=flatten_ack,
        reconciled_position=position,
        next_entry_allowed=next_entry_allowed,
        demo_order_writes_enabled=writes_enabled,
        account_taker_fee_rate=None if fee_rate is None else fee_rate.taker_fee_rate,
        account_maker_fee_rate=None if fee_rate is None else fee_rate.maker_fee_rate,
        live_mainnet_order_routing_allowed=False,
    )