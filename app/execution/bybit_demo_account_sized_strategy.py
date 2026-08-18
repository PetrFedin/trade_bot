from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.execution.bybit_demo_account_reader import (
    BybitDemoAccountInfo,
    BybitDemoWalletBalance,
)
from app.execution.bybit_demo_cycle import BybitDemoCyclePolicy
from app.execution.bybit_demo_strategy_selector import (
    BybitDemoStrategyCycleResult,
    execute_selected_reconciled_guarded_bybit_demo_cycle,
)
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.marketdata.bybit_v5 import BybitKlineBar
from app.strategy.crypto_perp import CryptoPerpStrategyConfig
from app.strategy.crypto_session_risk import CryptoSessionRiskState


class BybitDemoAccountSizedCycleStatus(StrEnum):
    ACCOUNT_STATE_BLOCKED = "ACCOUNT_STATE_BLOCKED"
    STRATEGY_CYCLE_CALLED = "STRATEGY_CYCLE_CALLED"


@dataclass(frozen=True)
class BybitDemoAccountSizedCycleResult:
    status: BybitDemoAccountSizedCycleStatus
    reasons: tuple[str, ...]
    strategy_cycle_result: BybitDemoStrategyCycleResult | None
    account_state_checked: bool
    wallet_balance: BybitDemoWalletBalance | None
    account_info: BybitDemoAccountInfo | None
    original_session_equity_usdt: Decimal
    effective_session_equity_usdt: Decimal
    effective_peak_equity_usdt: Decimal
    margin_mode: str | None
    demo_only: bool = True
    strategy_promotion_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False


StrategyCycleExecutor = Callable[..., BybitDemoStrategyCycleResult]


def execute_account_sized_reconciled_guarded_bybit_demo_cycle(
    bars_by_symbol: Mapping[str, Sequence[BybitKlineBar]],
    *,
    instruments: Mapping[str, BybitInstrumentSpec],
    strategy_config: CryptoPerpStrategyConfig,
    session_state: CryptoSessionRiskState,
    now: datetime,
    client: Any,
    accounting_client: Any | None,
    cycle_policy: BybitDemoCyclePolicy | None = None,
    strategy_cycle_executor: StrategyCycleExecutor = (
        execute_selected_reconciled_guarded_bybit_demo_cycle
    ),
    **strategy_cycle_kwargs: Any,
) -> BybitDemoAccountSizedCycleResult:
    """Refresh demo account equity/margin mode before any write-time strategy sizing.

    Research and dry-run calls preserve the supplied session state and do not require account
    access. Explicit demo writes fail closed unless the GET-only account reader proves a regular
    cross-margin UNIFIED account and returns a usable wallet snapshot. The refreshed total equity
    then becomes the sizing/current-equity input for the existing strategy-selection pipeline.
    """

    strategy_config.validate()
    session_state.validate()
    active_cycle_policy = BybitDemoCyclePolicy() if cycle_policy is None else cycle_policy
    active_cycle_policy.validate()

    if not active_cycle_policy.writes_enabled:
        result = strategy_cycle_executor(
            bars_by_symbol,
            instruments=instruments,
            strategy_config=strategy_config,
            session_state=session_state,
            now=now,
            client=client,
            accounting_client=accounting_client,
            cycle_policy=active_cycle_policy,
            **strategy_cycle_kwargs,
        )
        _reject_live_result(result)
        return BybitDemoAccountSizedCycleResult(
            status=BybitDemoAccountSizedCycleStatus.STRATEGY_CYCLE_CALLED,
            reasons=(),
            strategy_cycle_result=result,
            account_state_checked=False,
            wallet_balance=None,
            account_info=None,
            original_session_equity_usdt=session_state.current_equity_usdt,
            effective_session_equity_usdt=session_state.current_equity_usdt,
            effective_peak_equity_usdt=session_state.peak_equity_usdt,
            margin_mode=None,
        )

    if accounting_client is None:
        return _blocked(
            session_state,
            reasons=("DEMO_ACCOUNT_READER_REQUIRED_FOR_WRITES",),
        )
    if getattr(accounting_client, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("account-sized demo cycle rejected a mainnet-capable account reader")
    if getattr(accounting_client, "order_writes_supported", True) is not False:
        raise ValueError("account-sized demo cycle requires a GET-only account reader")

    try:
        account_info = accounting_client.get_account_info()
        account_info.validate()
        wallet = accounting_client.get_wallet_balance()
        wallet.validate()
    except Exception as exc:  # noqa: BLE001 - unresolved account state must block all writes.
        return _blocked(
            session_state,
            reasons=(f"DEMO_ACCOUNT_STATE_READ_FAILED:{type(exc).__name__}",),
            account_state_checked=True,
        )

    if account_info.margin_mode != "REGULAR_MARGIN":
        return _blocked(
            session_state,
            reasons=(
                f"DEMO_MARGIN_MODE_UNSUPPORTED_FOR_CURRENT_RISK_MODEL:{account_info.margin_mode}",
            ),
            account_state_checked=True,
            wallet_balance=wallet,
            account_info=account_info,
        )
    if wallet.total_available_balance_usd <= 0:
        return _blocked(
            session_state,
            reasons=("DEMO_AVAILABLE_BALANCE_NOT_POSITIVE",),
            account_state_checked=True,
            wallet_balance=wallet,
            account_info=account_info,
        )

    refreshed_state = replace(
        session_state,
        current_equity_usdt=wallet.total_equity_usd,
        peak_equity_usdt=max(session_state.peak_equity_usdt, wallet.total_equity_usd),
    )
    refreshed_state.validate()
    result = strategy_cycle_executor(
        bars_by_symbol,
        instruments=instruments,
        strategy_config=strategy_config,
        session_state=refreshed_state,
        now=now,
        client=client,
        accounting_client=accounting_client,
        cycle_policy=active_cycle_policy,
        **strategy_cycle_kwargs,
    )
    _reject_live_result(result)
    return BybitDemoAccountSizedCycleResult(
        status=BybitDemoAccountSizedCycleStatus.STRATEGY_CYCLE_CALLED,
        reasons=(),
        strategy_cycle_result=result,
        account_state_checked=True,
        wallet_balance=wallet,
        account_info=account_info,
        original_session_equity_usdt=session_state.current_equity_usdt,
        effective_session_equity_usdt=refreshed_state.current_equity_usdt,
        effective_peak_equity_usdt=refreshed_state.peak_equity_usdt,
        margin_mode=account_info.margin_mode,
    )


def _blocked(
    session_state: CryptoSessionRiskState,
    *,
    reasons: tuple[str, ...],
    account_state_checked: bool = False,
    wallet_balance: BybitDemoWalletBalance | None = None,
    account_info: BybitDemoAccountInfo | None = None,
) -> BybitDemoAccountSizedCycleResult:
    return BybitDemoAccountSizedCycleResult(
        status=BybitDemoAccountSizedCycleStatus.ACCOUNT_STATE_BLOCKED,
        reasons=reasons,
        strategy_cycle_result=None,
        account_state_checked=account_state_checked,
        wallet_balance=wallet_balance,
        account_info=account_info,
        original_session_equity_usdt=session_state.current_equity_usdt,
        effective_session_equity_usdt=session_state.current_equity_usdt,
        effective_peak_equity_usdt=session_state.peak_equity_usdt,
        margin_mode=None if account_info is None else account_info.margin_mode,
    )


def _reject_live_result(result: BybitDemoStrategyCycleResult) -> None:
    if result.live_mainnet_order_routing_allowed:
        raise ValueError("account-sized demo cycle received live mainnet permission")
