from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.execution.bybit_demo_approval_lineage import (
    BybitDemoApprovedEntryAuthorization,
    build_bybit_demo_approved_entry_authorization,
)
from app.execution.bybit_demo_approval_lineage_store import (
    BybitDemoApprovedEntryAuthorizationReceipt,
)
from app.execution.bybit_demo_approved_bridge import (
    execute_operator_approved_account_sized_bybit_demo_cycle,
)
from app.execution.bybit_demo_durable_approval_client import (
    DurableApprovalLineageBybitDemoClient,
)
from app.execution.bybit_demo_excursion_runtime import (
    BybitDemoExcursionRuntimeResult,
    initialize_bybit_demo_excursion_from_strategy_cycle,
)
from app.execution.bybit_demo_excursion_store import BybitDemoExcursionStore
from app.execution.bybit_demo_operator_approval import (
    BybitDemoOperatorApproval,
    dry_check_approved_opportunity_matches_demo_selector,
)
from app.execution.bybit_demo_protection_reconciliation import (
    execute_protection_reconciled_guarded_bybit_demo_cycle,
)
from app.execution.bybit_demo_ranked_fallback import (
    BybitDemoResilientAccountSizedCycleResult,
)
from app.execution.bybit_demo_trading_runtime import (
    BybitDemoTradingRuntimeResult,
    run_bybit_demo_trading_runtime,
)
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.marketdata.bybit_v5 import BybitKlineBar
from app.strategy.crypto_perp import CryptoPerpStrategyConfig
from app.strategy.crypto_session_risk import CryptoSessionRiskState


@dataclass(frozen=True)
class BybitDemoOperatorApprovedTradingRuntimeResult:
    runtime_result: BybitDemoTradingRuntimeResult
    authorization: BybitDemoApprovedEntryAuthorization | None
    authorization_receipt: BybitDemoApprovedEntryAuthorizationReceipt | None
    authorization_persisted: bool
    demo_only: bool = True
    strategy_promotion_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False


def run_operator_approved_bybit_demo_trading_runtime(
    approval: BybitDemoOperatorApproval,
    latest_review_row: Mapping[str, Any],
    bars_by_symbol: Mapping[str, Sequence[BybitKlineBar]],
    *,
    instruments: Mapping[str, BybitInstrumentSpec],
    strategy_config: CryptoPerpStrategyConfig,
    session_state: CryptoSessionRiskState,
    now: datetime,
    now_ms: int,
    client: Any,
    accounting_client: Any | None,
    excursion_store: BybitDemoExcursionStore,
    completed_bar_client: Any,
    quote_client: Any,
    runtime_lease: Any,
    approval_authorization_store: Any,
    terminal_evidence_store: Any | None = None,
    entry_provenance_store: Any | None = None,
    managed_policy: Any | None = None,
    managed_poller: Any | None = None,
    terminal_handoff: Any | None = None,
    build_entry_provenance: Any | None = None,
    **entry_kwargs: Any,
) -> BybitDemoOperatorApprovedTradingRuntimeResult:
    """Use one short-lived evidence approval only at the canonical new-entry boundary.

    The canonical runtime acquires the exclusive lease and checks durable active-trade state before
    this approval is consulted. If an active checkpoint exists, normal management/terminal handoff
    proceeds without requiring a still-valid approval. Only a genuinely new entry reaches the
    closure below.

    For a new entry, source identity and the canonical selector are first revalidated without a
    mutation. The approved runtime owns the write-time protection orchestrator exactly like the
    canonical resilient path: Demo writes require exchange protection-state reads and force the
    protection-reconciled orchestrator. A durable lineage client is placed underneath the existing
    single-use ``OperatorApprovedBybitDemoClient``. Account, fee, session-risk and fresh-quote
    checks remain free to reject the candidate without burning the approval. Only when the guarded
    stack is about to send the exact non-reduce-only Demo entry does the lower client atomically
    persist the outcome-free authorization. Existing durable authorization is recovery-only state
    and blocks resubmission. No ranked fallback to a different symbol is allowed.
    """

    _validate_authorization_store(approval_authorization_store)
    if "entry_executor" in entry_kwargs:
        raise ValueError("operator-approved runtime owns the entry_executor boundary")

    authorization_holder: list[BybitDemoApprovedEntryAuthorization] = []
    receipt_holder: list[BybitDemoApprovedEntryAuthorizationReceipt] = []

    def approved_entry_executor(
        inner_bars: Mapping[str, Sequence[BybitKlineBar]],
        **inner_kwargs: Any,
    ) -> BybitDemoResilientAccountSizedCycleResult:
        inner = dict(inner_kwargs)
        inner_instruments = inner.pop("instruments")
        inner_strategy_config = inner.pop("strategy_config")
        inner_session_state = inner.pop("session_state")
        inner_now = inner.pop("now")
        inner_client = inner.pop("client")
        inner_accounting_client = inner.pop("accounting_client")
        inner_excursion_store = inner.pop("excursion_store")

        if "orchestrator" in inner:
            raise ValueError("operator-approved runtime owns the protection orchestrator boundary")
        if getattr(inner_client, "protection_state_read_supported", False) is not True:
            raise ValueError(
                "operator-approved Demo writes require protection-state read capability"
            )
        inner["orchestrator"] = execute_protection_reconciled_guarded_bybit_demo_cycle

        authorization = build_bybit_demo_approved_entry_authorization(
            approval,
            latest_review_row,
            now=inner_now,
        )
        dry_check_approved_opportunity_matches_demo_selector(
            approval,
            latest_review_row,
            inner_bars,
            instruments=inner_instruments,
            strategy_config=inner_strategy_config,
            session_state=inner_session_state,
            now=inner_now,
        )
        authorization_holder.append(authorization)
        durable_client = DurableApprovalLineageBybitDemoClient(
            inner_client,
            approval,
            authorization,
            store=approval_authorization_store,
            on_persisted=receipt_holder.append,
        )
        if not durable_client.protection_state_read_supported:
            raise ValueError("durable approved client lost protection-state read capability")

        account_result = execute_operator_approved_account_sized_bybit_demo_cycle(
            approval,
            latest_review_row,
            inner_bars,
            instruments=inner_instruments,
            strategy_config=inner_strategy_config,
            session_state=inner_session_state,
            now=inner_now,
            client=durable_client,
            accounting_client=inner_accounting_client,
            **inner,
        )
        _reject_live(account_result, name="approved account-sized cycle")
        return _wrap_approved_account_result(
            account_result,
            approval=approval,
            excursion_store=inner_excursion_store,
        )

    runtime_kwargs: dict[str, Any] = dict(entry_kwargs)
    if managed_poller is not None:
        runtime_kwargs["managed_poller"] = managed_poller
    if terminal_handoff is not None:
        runtime_kwargs["terminal_handoff"] = terminal_handoff
    if build_entry_provenance is not None:
        runtime_kwargs["build_entry_provenance"] = build_entry_provenance

    runtime = run_bybit_demo_trading_runtime(
        bars_by_symbol,
        instruments=instruments,
        strategy_config=strategy_config,
        session_state=session_state,
        now=now,
        now_ms=now_ms,
        client=client,
        accounting_client=accounting_client,
        excursion_store=excursion_store,
        completed_bar_client=completed_bar_client,
        quote_client=quote_client,
        runtime_lease=runtime_lease,
        terminal_evidence_store=terminal_evidence_store,
        entry_provenance_store=entry_provenance_store,
        managed_policy=managed_policy,
        entry_executor=approved_entry_executor,
        **runtime_kwargs,
    )
    _reject_live(runtime, name="operator-approved trading runtime")
    if len(authorization_holder) > 1 or len(receipt_holder) > 1:
        raise ValueError("operator-approved runtime attempted more than one authorization")
    authorization = authorization_holder[0] if authorization_holder else None
    receipt = receipt_holder[0] if receipt_holder else None
    return BybitDemoOperatorApprovedTradingRuntimeResult(
        runtime_result=runtime,
        authorization=authorization,
        authorization_receipt=receipt,
        authorization_persisted=receipt is not None,
    )


def _wrap_approved_account_result(
    account_result: Any,
    *,
    approval: BybitDemoOperatorApproval,
    excursion_store: BybitDemoExcursionStore,
) -> BybitDemoResilientAccountSizedCycleResult:
    strategy_result = account_result.strategy_cycle_result
    tracking: BybitDemoExcursionRuntimeResult | None = None
    final_symbol = None
    if strategy_result is not None:
        tracking = initialize_bybit_demo_excursion_from_strategy_cycle(
            strategy_result,
            store=excursion_store,
        )
        _reject_live(tracking, name="approved excursion tracking")
        selected = strategy_result.selection.selected_trade_plan
        if selected is not None:
            if selected.symbol != approval.symbol:
                raise ValueError("approved runtime selected another symbol")
            if selected.side.value != approval.side:
                raise ValueError("approved runtime selected another side")
            if selected.decision_time != approval.decision_time:
                raise ValueError("approved runtime selected another decision time")
            final_symbol = selected.symbol
    return BybitDemoResilientAccountSizedCycleResult(
        account_sized_result=account_result,
        fallback_attempts=(),
        selected_after_fallback=False,
        candidates_exhausted=False,
        final_selected_symbol=final_symbol,
        excursion_tracking_result=tracking,
        demo_only=True,
        strategy_promotion_allowed=False,
        live_mainnet_order_routing_allowed=False,
    )


def _validate_authorization_store(store: Any) -> None:
    if getattr(store, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("operator-approved runtime rejected mainnet-capable authorization store")
    if getattr(store, "order_writes_supported", True) is not False:
        raise ValueError("operator-approved runtime requires non-trading authorization store")
    if getattr(store, "order_submission_supported", True) is not False:
        raise ValueError("operator-approved runtime authorization store cannot submit orders")
    if getattr(store, "immutable_records", False) is not True:
        raise ValueError("operator-approved runtime requires immutable authorization store")
    if getattr(store, "outcome_storage_allowed", True) is not False:
        raise ValueError("operator-approved runtime authorization store must be outcome-free")
    if getattr(store, "realized_pnl_storage_allowed", True) is not False:
        raise ValueError("operator-approved runtime authorization store cannot store realized PnL")


def _reject_live(value: Any, *, name: str) -> None:
    if getattr(value, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError(f"operator-approved runtime rejected mainnet-capable {name}")


__all__ = [
    "BybitDemoOperatorApprovedTradingRuntimeResult",
    "run_operator_approved_bybit_demo_trading_runtime",
]
