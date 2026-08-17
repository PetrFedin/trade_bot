from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.execution.bybit_demo_cycle import (
    BybitDemoCyclePolicy,
    BybitDemoCycleResult,
    execute_bybit_demo_trade_cycle,
)
from app.execution.bybit_demo_lifecycle_gate import BybitDemoLifecycleDecision
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.strategy.crypto_perp import CryptoPerpStrategyConfig, CryptoTradePlan
from app.strategy.crypto_session_risk import CryptoSessionRiskPolicy, CryptoSessionRiskState


class BybitDemoOrchestratorStatus(StrEnum):
    PREVIOUS_TRADE_RECONCILIATION_BLOCKED = "PREVIOUS_TRADE_RECONCILIATION_BLOCKED"
    CYCLE_EXECUTED = "CYCLE_EXECUTED"


@dataclass(frozen=True)
class BybitDemoOrchestratorResult:
    status: BybitDemoOrchestratorStatus
    reasons: tuple[str, ...]
    cycle_result: BybitDemoCycleResult | None
    previous_trade_gate_checked: bool
    next_entry_allowed: bool
    demo_only: bool = True
    strategy_promotion_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False


CycleExecutor = Callable[..., BybitDemoCycleResult]
Sleeper = Callable[[float], None]


def execute_guarded_bybit_demo_cycle(
    trade_plan: CryptoTradePlan,
    *,
    instrument: BybitInstrumentSpec,
    strategy_config: CryptoPerpStrategyConfig,
    session_state: CryptoSessionRiskState,
    client: Any,
    previous_trade_lifecycle: BybitDemoLifecycleDecision | None = None,
    cycle_policy: BybitDemoCyclePolicy | None = None,
    session_policy: CryptoSessionRiskPolicy | None = None,
    sleeper: Sleeper = time.sleep,
    cycle_executor: CycleExecutor = execute_bybit_demo_trade_cycle,
) -> BybitDemoOrchestratorResult:
    """Execute a demo cycle only after prior symbol lifecycle reconciliation permits reuse."""

    if previous_trade_lifecycle is not None and not previous_trade_lifecycle.next_entry_allowed:
        return BybitDemoOrchestratorResult(
            status=BybitDemoOrchestratorStatus.PREVIOUS_TRADE_RECONCILIATION_BLOCKED,
            reasons=(
                "PREVIOUS_TRADE_LIFECYCLE_BLOCKED",
                *previous_trade_lifecycle.reasons,
            ),
            cycle_result=None,
            previous_trade_gate_checked=True,
            next_entry_allowed=False,
            demo_only=True,
            strategy_promotion_allowed=False,
            live_mainnet_order_routing_allowed=False,
        )

    cycle = cycle_executor(
        trade_plan,
        instrument=instrument,
        strategy_config=strategy_config,
        session_state=session_state,
        client=client,
        cycle_policy=cycle_policy,
        session_policy=session_policy,
        sleeper=sleeper,
    )
    return BybitDemoOrchestratorResult(
        status=BybitDemoOrchestratorStatus.CYCLE_EXECUTED,
        reasons=cycle.reasons,
        cycle_result=cycle,
        previous_trade_gate_checked=previous_trade_lifecycle is not None,
        next_entry_allowed=cycle.next_entry_allowed,
        demo_only=True,
        strategy_promotion_allowed=False,
        live_mainnet_order_routing_allowed=False,
    )
