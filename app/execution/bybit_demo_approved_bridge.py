from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from app.execution.bybit_demo_account_sized_strategy import (
    BybitDemoAccountSizedCycleResult,
    execute_account_sized_reconciled_guarded_bybit_demo_cycle,
)
from app.execution.bybit_demo_cycle import BybitDemoCyclePolicy
from app.execution.bybit_demo_funding_reconciliation import BybitDemoFundingLedgerWindow
from app.execution.bybit_demo_lifecycle_gate import BybitDemoLifecyclePolicy
from app.execution.bybit_demo_operator_approval import (
    BybitDemoOperatorApproval,
    OperatorApprovedBybitDemoClient,
    dry_check_approved_opportunity_matches_demo_selector,
    validate_demo_approval_against_latest_review_row,
)
from app.execution.bybit_demo_orchestrator import BybitDemoPreviousTradeReference
from app.execution.bybit_demo_session_risk_ledger import BybitDemoSessionRiskLedger
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.marketdata.bybit_v5 import BybitKlineBar
from app.strategy.crypto_perp import CryptoPerpStrategyConfig
from app.strategy.crypto_session_risk import CryptoSessionRiskState


def execute_operator_approved_account_sized_bybit_demo_cycle(
    approval: BybitDemoOperatorApproval,
    latest_review_row: Mapping[str, Any],
    bars_by_symbol: Mapping[str, Sequence[BybitKlineBar]],
    *,
    instruments: Mapping[str, BybitInstrumentSpec],
    strategy_config: CryptoPerpStrategyConfig,
    session_state: CryptoSessionRiskState,
    now: datetime,
    client: Any,
    accounting_client: Any,
    session_ledger: BybitDemoSessionRiskLedger,
    cycle_policy: BybitDemoCyclePolicy,
    previous_trade: BybitDemoPreviousTradeReference | None = None,
    trade_read_client: Any | None = None,
    funding_ledger: BybitDemoFundingLedgerWindow | None = None,
    lifecycle_policy: BybitDemoLifecyclePolicy | None = None,
    **strategy_cycle_kwargs: Any,
) -> BybitDemoAccountSizedCycleResult:
    """Execute one explicitly approved evidence-ranked opportunity on Bybit Demo only.

    The approval is checked against the latest review row and the current dry-run demo selector
    before any account refresh or order write. The real account-sized runtime still performs its
    normal wallet/margin/session-ledger/previous-trade checks. Its order client is wrapped by a
    single-use exact-identity guard, so a race that changes selection after the dry check cannot
    send a different symbol, side, decision or larger quantity to the network.
    """

    strategy_config.validate()
    if strategy_config != CryptoPerpStrategyConfig():
        raise ValueError("approved demo execution requires the qualified fixed strategy config")
    session_state.validate()
    cycle_policy.validate()
    if not cycle_policy.writes_enabled:
        raise ValueError("approved demo execution requires explicit demo writes_enabled=true")
    validate_demo_approval_against_latest_review_row(
        approval,
        latest_review_row,
        now=now,
    )
    dry_check_approved_opportunity_matches_demo_selector(
        approval,
        latest_review_row,
        bars_by_symbol,
        instruments=instruments,
        strategy_config=strategy_config,
        session_state=session_state,
        now=now,
    )
    guarded_client = OperatorApprovedBybitDemoClient(client, approval, now=now)
    result = execute_account_sized_reconciled_guarded_bybit_demo_cycle(
        bars_by_symbol,
        instruments=instruments,
        strategy_config=strategy_config,
        session_state=session_state,
        now=now,
        client=guarded_client,
        accounting_client=accounting_client,
        cycle_policy=cycle_policy,
        session_ledger=session_ledger,
        previous_trade=previous_trade,
        trade_read_client=trade_read_client,
        funding_ledger=funding_ledger,
        lifecycle_policy=lifecycle_policy,
        **strategy_cycle_kwargs,
    )
    if result.live_mainnet_order_routing_allowed:
        raise ValueError("approved demo bridge received live mainnet permission")
    strategy_result = result.strategy_cycle_result
    if strategy_result is not None:
        if strategy_result.live_mainnet_order_routing_allowed:
            raise ValueError("approved demo bridge strategy result enabled mainnet routing")
        selected = strategy_result.selection.selected_trade_plan
        if selected is not None:
            if selected.symbol != approval.symbol:
                raise ValueError("approved demo bridge selected another symbol")
            if selected.side.value != approval.side:
                raise ValueError("approved demo bridge selected another side")
            if selected.decision_time != approval.decision_time:
                raise ValueError("approved demo bridge selected another decision time")
            if selected.reference_quantity > approval.maximum_entry_quantity:
                raise ValueError("approved demo bridge selected quantity above approval")
    return result


__all__ = ["execute_operator_approved_account_sized_bybit_demo_cycle"]
