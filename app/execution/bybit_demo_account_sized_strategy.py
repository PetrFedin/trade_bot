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
from app.execution.bybit_demo_funding_reconciliation import BybitDemoFundingLedgerWindow
from app.execution.bybit_demo_lifecycle_gate import BybitDemoLifecyclePolicy
from app.execution.bybit_demo_orchestrator import BybitDemoPreviousTradeReference
from app.execution.bybit_demo_post_trade_accounting import (
    BybitDemoPostTradeAccountingResult,
    reconcile_bybit_demo_trade_lifecycle,
)
from app.execution.bybit_demo_session_risk_ledger import (
    BybitDemoSessionRiskLedger,
    apply_fully_reconciled_trade_to_session_ledger,
)
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
    SESSION_RISK_STATE_BLOCKED = "SESSION_RISK_STATE_BLOCKED"
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
    session_ledger_checked: bool = False
    session_ledger: BybitDemoSessionRiskLedger | None = None
    previous_trade_accounting: BybitDemoPostTradeAccountingResult | None = None
    demo_only: bool = True
    strategy_promotion_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False


StrategyCycleExecutor = Callable[..., BybitDemoStrategyCycleResult]
PreviousTradeReconciler = Callable[..., BybitDemoPostTradeAccountingResult]


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
    session_ledger: BybitDemoSessionRiskLedger | None = None,
    previous_trade: BybitDemoPreviousTradeReference | None = None,
    trade_read_client: Any | None = None,
    funding_ledger: BybitDemoFundingLedgerWindow | None = None,
    lifecycle_policy: BybitDemoLifecyclePolicy | None = None,
    previous_trade_reconciler: PreviousTradeReconciler = (
        reconcile_bybit_demo_trade_lifecycle
    ),
    strategy_cycle_executor: StrategyCycleExecutor = (
        execute_selected_reconciled_guarded_bybit_demo_cycle
    ),
    **strategy_cycle_kwargs: Any,
) -> BybitDemoAccountSizedCycleResult:
    """Refresh authoritative account/session risk state before any write-time selection.

    Research and dry-run calls preserve the supplied session state. Explicit demo writes require
    a GET-only account reader plus a reconciled session-risk ledger. If a previous trade is
    supplied, its all-in lifecycle is reconciled and applied to the ledger before signal ranking.
    This prevents stale external loss-streak/cost/PnL counters from authorizing another entry.
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
            previous_trade=previous_trade,
            trade_read_client=trade_read_client,
            funding_ledger=funding_ledger,
            lifecycle_policy=lifecycle_policy,
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
            session_ledger_checked=False,
            session_ledger=session_ledger,
        )

    if accounting_client is None:
        return _blocked(
            session_state,
            status=BybitDemoAccountSizedCycleStatus.ACCOUNT_STATE_BLOCKED,
            reasons=("DEMO_ACCOUNT_READER_REQUIRED_FOR_WRITES",),
        )
    if getattr(accounting_client, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("account-sized demo cycle rejected a mainnet-capable account reader")
    if getattr(accounting_client, "order_writes_supported", True) is not False:
        raise ValueError("account-sized demo cycle requires a GET-only account reader")
    if session_ledger is None:
        return _blocked(
            session_state,
            status=BybitDemoAccountSizedCycleStatus.SESSION_RISK_STATE_BLOCKED,
            reasons=("DEMO_SESSION_RISK_LEDGER_REQUIRED_FOR_WRITES",),
        )
    try:
        session_ledger.validate()
    except ValueError as exc:
        return _blocked(
            session_state,
            status=BybitDemoAccountSizedCycleStatus.SESSION_RISK_STATE_BLOCKED,
            reasons=(f"DEMO_SESSION_RISK_LEDGER_INVALID:{type(exc).__name__}",),
            session_ledger_checked=True,
            session_ledger=session_ledger,
        )
    if session_ledger.opening_equity_usdt != session_state.opening_equity_usdt:
        return _blocked(
            session_state,
            status=BybitDemoAccountSizedCycleStatus.SESSION_RISK_STATE_BLOCKED,
            reasons=("DEMO_SESSION_OPENING_EQUITY_MISMATCH",),
            session_ledger_checked=True,
            session_ledger=session_ledger,
        )

    try:
        account_info = accounting_client.get_account_info()
        account_info.validate()
        wallet = accounting_client.get_wallet_balance()
        wallet.validate()
    except Exception as exc:  # noqa: BLE001 - unresolved account state must block all writes.
        return _blocked(
            session_state,
            status=BybitDemoAccountSizedCycleStatus.ACCOUNT_STATE_BLOCKED,
            reasons=(f"DEMO_ACCOUNT_STATE_READ_FAILED:{type(exc).__name__}",),
            account_state_checked=True,
            session_ledger_checked=True,
            session_ledger=session_ledger,
        )

    if account_info.margin_mode != "REGULAR_MARGIN":
        return _blocked(
            session_state,
            status=BybitDemoAccountSizedCycleStatus.ACCOUNT_STATE_BLOCKED,
            reasons=(
                f"DEMO_MARGIN_MODE_UNSUPPORTED_FOR_CURRENT_RISK_MODEL:{account_info.margin_mode}",
            ),
            account_state_checked=True,
            wallet_balance=wallet,
            account_info=account_info,
            session_ledger_checked=True,
            session_ledger=session_ledger,
        )
    if wallet.total_available_balance_usd <= 0:
        return _blocked(
            session_state,
            status=BybitDemoAccountSizedCycleStatus.ACCOUNT_STATE_BLOCKED,
            reasons=("DEMO_AVAILABLE_BALANCE_NOT_POSITIVE",),
            account_state_checked=True,
            wallet_balance=wallet,
            account_info=account_info,
            session_ledger_checked=True,
            session_ledger=session_ledger,
        )

    active_ledger = session_ledger
    previous_accounting = None
    if previous_trade is not None:
        if trade_read_client is None:
            return _blocked(
                session_state,
                status=BybitDemoAccountSizedCycleStatus.SESSION_RISK_STATE_BLOCKED,
                reasons=("DEMO_PREVIOUS_TRADE_READER_REQUIRED_FOR_SESSION_LEDGER",),
                account_state_checked=True,
                wallet_balance=wallet,
                account_info=account_info,
                session_ledger_checked=True,
                session_ledger=active_ledger,
            )
        try:
            previous_accounting = previous_trade_reconciler(
                trade_client=trade_read_client,
                accounting_client=accounting_client,
                symbol=previous_trade.symbol,
                entry_side=previous_trade.entry_side,
                entry_order_link_id=previous_trade.entry_order_link_id,
                execution_limit=previous_trade.execution_limit,
                funding_ledger=funding_ledger,
                lifecycle_policy=lifecycle_policy,
            )
        except Exception as exc:  # noqa: BLE001 - previous trade must resolve before selection.
            return _blocked(
                session_state,
                status=BybitDemoAccountSizedCycleStatus.SESSION_RISK_STATE_BLOCKED,
                reasons=(f"DEMO_PREVIOUS_TRADE_RECONCILIATION_FAILED:{type(exc).__name__}",),
                account_state_checked=True,
                wallet_balance=wallet,
                account_info=account_info,
                session_ledger_checked=True,
                session_ledger=active_ledger,
            )
        if not previous_accounting.lifecycle.next_entry_allowed:
            return _blocked(
                session_state,
                status=BybitDemoAccountSizedCycleStatus.SESSION_RISK_STATE_BLOCKED,
                reasons=(
                    "DEMO_PREVIOUS_TRADE_LIFECYCLE_BLOCKED_BEFORE_SELECTION",
                    *previous_accounting.lifecycle.reasons,
                ),
                account_state_checked=True,
                wallet_balance=wallet,
                account_info=account_info,
                session_ledger_checked=True,
                session_ledger=active_ledger,
                previous_trade_accounting=previous_accounting,
            )
        try:
            active_ledger = apply_fully_reconciled_trade_to_session_ledger(
                active_ledger,
                previous_accounting,
            )
        except ValueError as exc:
            return _blocked(
                session_state,
                status=BybitDemoAccountSizedCycleStatus.SESSION_RISK_STATE_BLOCKED,
                reasons=(f"DEMO_SESSION_LEDGER_APPLY_FAILED:{type(exc).__name__}",),
                account_state_checked=True,
                wallet_balance=wallet,
                account_info=account_info,
                session_ledger_checked=True,
                session_ledger=active_ledger,
                previous_trade_accounting=previous_accounting,
            )

    refreshed_state = active_ledger.to_session_risk_state(
        current_equity_usdt=wallet.total_equity_usd
    )
    if session_state.peak_equity_usdt > refreshed_state.peak_equity_usdt:
        refreshed_state = replace(
            refreshed_state,
            peak_equity_usdt=session_state.peak_equity_usdt,
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
        previous_trade=None,
        trade_read_client=trade_read_client,
        funding_ledger=funding_ledger,
        lifecycle_policy=lifecycle_policy,
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
        session_ledger_checked=True,
        session_ledger=active_ledger,
        previous_trade_accounting=previous_accounting,
    )


def _blocked(
    session_state: CryptoSessionRiskState,
    *,
    status: BybitDemoAccountSizedCycleStatus,
    reasons: tuple[str, ...],
    account_state_checked: bool = False,
    wallet_balance: BybitDemoWalletBalance | None = None,
    account_info: BybitDemoAccountInfo | None = None,
    session_ledger_checked: bool = False,
    session_ledger: BybitDemoSessionRiskLedger | None = None,
    previous_trade_accounting: BybitDemoPostTradeAccountingResult | None = None,
) -> BybitDemoAccountSizedCycleResult:
    return BybitDemoAccountSizedCycleResult(
        status=status,
        reasons=reasons,
        strategy_cycle_result=None,
        account_state_checked=account_state_checked,
        wallet_balance=wallet_balance,
        account_info=account_info,
        original_session_equity_usdt=session_state.current_equity_usdt,
        effective_session_equity_usdt=session_state.current_equity_usdt,
        effective_peak_equity_usdt=session_state.peak_equity_usdt,
        margin_mode=None if account_info is None else account_info.margin_mode,
        session_ledger_checked=session_ledger_checked,
        session_ledger=session_ledger,
        previous_trade_accounting=previous_trade_accounting,
    )


def _reject_live_result(result: BybitDemoStrategyCycleResult) -> None:
    if result.live_mainnet_order_routing_allowed:
        raise ValueError("account-sized demo cycle received live mainnet permission")
