from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.execution.bybit_demo_account_sized_strategy import (
    BybitDemoAccountSizedCycleResult,
    execute_account_sized_reconciled_guarded_bybit_demo_cycle,
)
from app.execution.bybit_demo_cycle import BybitDemoCyclePolicy, BybitDemoCycleStatus
from app.execution.bybit_demo_excursion_runtime import (
    BybitDemoExcursionRuntimeResult,
    BybitDemoExcursionRuntimeStatus,
    initialize_bybit_demo_excursion_from_strategy_cycle,
)
from app.execution.bybit_demo_excursion_store import BybitDemoExcursionStore
from app.execution.bybit_demo_orchestrator import BybitDemoOrchestratorStatus
from app.execution.bybit_demo_protection_reconciliation import (
    execute_protection_reconciled_guarded_bybit_demo_cycle,
)
from app.execution.bybit_demo_strategy_selector import (
    BybitDemoStrategyCycleResult,
    BybitDemoStrategyCycleStatus,
    execute_selected_reconciled_guarded_bybit_demo_cycle,
)
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.marketdata.bybit_v5 import BybitKlineBar
from app.strategy.crypto_perp import CryptoPerpStrategyConfig
from app.strategy.crypto_session_risk import CryptoSessionRiskState

_ACCOUNT_FEE_RETRYABLE_REASONS = {
    "ACCOUNT_FEE_TARGET_NET_EDGE_UNAVAILABLE",
    "ACCOUNT_FEE_EXPECTED_NET_PROFIT_BELOW_TARGET",
    "ACCOUNT_FEE_RISK_BUDGET_EXCEEDED",
}


class BybitDemoCandidateFallbackStage(StrEnum):
    PRE_ENTRY_QUOTE = "PRE_ENTRY_QUOTE"
    ACCOUNT_FEE_ECONOMICS = "ACCOUNT_FEE_ECONOMICS"


@dataclass(frozen=True)
class BybitDemoCandidateFallbackAttempt:
    symbol: str
    side: str
    stage: BybitDemoCandidateFallbackStage
    reasons: tuple[str, ...]
    quote_price: Decimal | None
    modeled_entry_price: Decimal | None


@dataclass(frozen=True)
class BybitDemoRankedFallbackResult:
    cycle_result: BybitDemoStrategyCycleResult
    fallback_attempts: tuple[BybitDemoCandidateFallbackAttempt, ...]
    selected_after_fallback: bool
    candidates_exhausted: bool
    final_selected_symbol: str | None
    demo_only: bool = True
    strategy_promotion_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False


@dataclass(frozen=True)
class BybitDemoResilientAccountSizedCycleResult:
    account_sized_result: BybitDemoAccountSizedCycleResult
    fallback_attempts: tuple[BybitDemoCandidateFallbackAttempt, ...]
    selected_after_fallback: bool
    candidates_exhausted: bool
    final_selected_symbol: str | None
    excursion_tracking_result: BybitDemoExcursionRuntimeResult | None = None
    demo_only: bool = True
    strategy_promotion_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False


StrategyCycleExecutor = Callable[..., BybitDemoStrategyCycleResult]
AccountSizedExecutor = Callable[..., BybitDemoAccountSizedCycleResult]


def run_ranked_fallback_bybit_demo_strategy_cycle(
    bars_by_symbol: Mapping[str, Sequence[BybitKlineBar]],
    *,
    instruments: Mapping[str, BybitInstrumentSpec],
    strategy_config: CryptoPerpStrategyConfig,
    session_state: CryptoSessionRiskState,
    now: datetime,
    client: Any,
    base_executor: StrategyCycleExecutor = (
        execute_selected_reconciled_guarded_bybit_demo_cycle
    ),
    **strategy_cycle_kwargs: Any,
) -> BybitDemoRankedFallbackResult:
    """Retry the next ranked executable symbol after deterministic pre-order rejection.

    The retry surface is intentionally narrow. A candidate can be skipped only when the fresh
    executable quote destroys its already-required edge/instrument viability, or when the real
    account taker fee deterministically invalidates edge/risk before an entry acknowledgement.
    Missing quote data, fee API failures, portfolio failures, lifecycle blocks and any state after
    an entry acknowledgement remain fail-closed and are never converted into another attempt.
    Thresholds are not relaxed between attempts.
    """

    effective_instruments = dict(instruments)
    attempts: list[BybitDemoCandidateFallbackAttempt] = []
    last_result: BybitDemoStrategyCycleResult | None = None

    for _attempt_index in range(len(effective_instruments) + 1):
        result = base_executor(
            bars_by_symbol,
            instruments=effective_instruments,
            strategy_config=strategy_config,
            session_state=session_state,
            now=now,
            client=client,
            **strategy_cycle_kwargs,
        )
        _reject_live_cycle(result)
        last_result = result

        plan = result.selection.selected_trade_plan
        if _retryable_quote_block(result):
            if plan is None:
                raise ValueError("retryable quote block is missing its selected trade plan")
            attempts.append(
                BybitDemoCandidateFallbackAttempt(
                    symbol=plan.symbol,
                    side=plan.side.value,
                    stage=BybitDemoCandidateFallbackStage.PRE_ENTRY_QUOTE,
                    reasons=result.pre_entry_quote_reasons,
                    quote_price=result.pre_entry_quote_price,
                    modeled_entry_price=result.pre_entry_modeled_entry_price,
                )
            )
            if not _exclude_instrument(effective_instruments, plan.symbol):
                return _ranked_result(result, attempts, candidates_exhausted=False)
            if not effective_instruments:
                return _ranked_result(result, attempts, candidates_exhausted=True)
            continue

        fee_reasons = _retryable_account_fee_reasons(result)
        if fee_reasons is not None:
            if plan is None:
                raise ValueError("retryable account-fee block is missing its selected trade plan")
            attempts.append(
                BybitDemoCandidateFallbackAttempt(
                    symbol=plan.symbol,
                    side=plan.side.value,
                    stage=BybitDemoCandidateFallbackStage.ACCOUNT_FEE_ECONOMICS,
                    reasons=fee_reasons,
                    quote_price=result.pre_entry_quote_price,
                    modeled_entry_price=result.pre_entry_modeled_entry_price,
                )
            )
            if not _exclude_instrument(effective_instruments, plan.symbol):
                return _ranked_result(result, attempts, candidates_exhausted=False)
            if not effective_instruments:
                return _ranked_result(result, attempts, candidates_exhausted=True)
            continue

        exhausted = bool(attempts) and (
            result.status is BybitDemoStrategyCycleStatus.NO_TRADE
            or result.selection.selected_trade_plan is None
        )
        return _ranked_result(result, attempts, candidates_exhausted=exhausted)

    if last_result is None:
        raise AssertionError("ranked fallback executor did not produce a cycle result")
    return _ranked_result(last_result, attempts, candidates_exhausted=True)


def execute_resilient_account_sized_reconciled_guarded_bybit_demo_cycle(
    bars_by_symbol: Mapping[str, Sequence[BybitKlineBar]],
    *,
    instruments: Mapping[str, BybitInstrumentSpec],
    strategy_config: CryptoPerpStrategyConfig,
    session_state: CryptoSessionRiskState,
    now: datetime,
    client: Any,
    accounting_client: Any | None,
    excursion_store: BybitDemoExcursionStore | None = None,
    account_sized_executor: AccountSizedExecutor = (
        execute_account_sized_reconciled_guarded_bybit_demo_cycle
    ),
    base_strategy_executor: StrategyCycleExecutor = (
        execute_selected_reconciled_guarded_bybit_demo_cycle
    ),
    **account_sized_kwargs: Any,
) -> BybitDemoResilientAccountSizedCycleResult:
    """Canonical account-refreshed demo path with bounded ranked pre-order fallback.

    Explicit writes additionally require protection-aware position reads and automatically use
    the protection-reconciled orchestrator. If a diagnostics-only excursion store is supplied,
    a successfully protected position is checkpointed after the strategy cycle so later mark
    observations can preserve MFE/MAE/giveback across restarts. Tracking never alters the already
    completed order/protection result and carries no strategy-promotion or mainnet permission.
    """

    if "strategy_cycle_executor" in account_sized_kwargs:
        raise ValueError("resilient demo cycle owns the strategy_cycle_executor boundary")
    _validate_optional_excursion_store(excursion_store)

    cycle_policy = account_sized_kwargs.get("cycle_policy")
    active_cycle_policy = BybitDemoCyclePolicy() if cycle_policy is None else cycle_policy
    active_cycle_policy.validate()
    if active_cycle_policy.writes_enabled:
        if "orchestrator" in account_sized_kwargs:
            raise ValueError("resilient demo writes own the protection orchestrator boundary")
        if not getattr(client, "protection_state_read_supported", False):
            raise ValueError(
                "resilient demo writes require protection-state read capability"
            )
        account_sized_kwargs["orchestrator"] = (
            execute_protection_reconciled_guarded_bybit_demo_cycle
        )

    ranked_holder: list[BybitDemoRankedFallbackResult] = []

    def fallback_executor(
        inner_bars: Mapping[str, Sequence[BybitKlineBar]],
        **inner_kwargs: Any,
    ) -> BybitDemoStrategyCycleResult:
        ranked = run_ranked_fallback_bybit_demo_strategy_cycle(
            inner_bars,
            base_executor=base_strategy_executor,
            **inner_kwargs,
        )
        ranked_holder.append(ranked)
        return ranked.cycle_result

    account_result = account_sized_executor(
        bars_by_symbol,
        instruments=instruments,
        strategy_config=strategy_config,
        session_state=session_state,
        now=now,
        client=client,
        accounting_client=accounting_client,
        strategy_cycle_executor=fallback_executor,
        **account_sized_kwargs,
    )
    if account_result.live_mainnet_order_routing_allowed:
        raise ValueError("resilient demo cycle received live mainnet permission")
    if len(ranked_holder) > 1:
        raise ValueError("account-sized demo executor called strategy selection more than once")

    ranked = ranked_holder[0] if ranked_holder else None
    excursion_tracking = _initialize_optional_excursion_tracking(
        account_result,
        excursion_store=excursion_store,
    )
    return BybitDemoResilientAccountSizedCycleResult(
        account_sized_result=account_result,
        fallback_attempts=() if ranked is None else ranked.fallback_attempts,
        selected_after_fallback=False if ranked is None else ranked.selected_after_fallback,
        candidates_exhausted=False if ranked is None else ranked.candidates_exhausted,
        final_selected_symbol=None if ranked is None else ranked.final_selected_symbol,
        excursion_tracking_result=excursion_tracking,
        demo_only=True,
        strategy_promotion_allowed=False,
        live_mainnet_order_routing_allowed=False,
    )


def summarize_bybit_demo_ranked_fallback_quality(
    results: Sequence[BybitDemoResilientAccountSizedCycleResult],
) -> dict[str, Any]:
    stage_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    rejected_symbol_counts: Counter[str] = Counter()
    final_symbol_counts: Counter[str] = Counter()
    excursion_status_counts: Counter[str] = Counter()
    selected_after_fallback_count = 0
    candidates_exhausted_count = 0

    for result in results:
        if result.live_mainnet_order_routing_allowed:
            raise ValueError("ranked fallback quality rejected mainnet-capable result")
        if result.account_sized_result.live_mainnet_order_routing_allowed:
            raise ValueError("ranked fallback quality rejected mainnet-capable account result")
        if result.selected_after_fallback:
            selected_after_fallback_count += 1
        if result.candidates_exhausted:
            candidates_exhausted_count += 1
        if result.final_selected_symbol is not None:
            final_symbol_counts[result.final_selected_symbol] += 1
        excursion = getattr(result, "excursion_tracking_result", None)
        if excursion is not None:
            if excursion.live_mainnet_order_routing_allowed:
                raise ValueError("ranked fallback quality rejected live excursion diagnostics")
            excursion_status_counts[excursion.status.value] += 1
        for attempt in result.fallback_attempts:
            stage_counts[attempt.stage.value] += 1
            reason_counts.update(attempt.reasons)
            rejected_symbol_counts[attempt.symbol] += 1

    return {
        "qualification": "BYBIT_DEMO_RANKED_PRE_ORDER_FALLBACK_QUALITY",
        "cycle_count": len(results),
        "fallback_attempt_count": sum(stage_counts.values()),
        "selected_after_fallback_count": selected_after_fallback_count,
        "candidates_exhausted_count": candidates_exhausted_count,
        "fallback_stage_counts": dict(sorted(stage_counts.items())),
        "fallback_reason_counts": dict(sorted(reason_counts.items())),
        "rejected_symbol_counts": dict(sorted(rejected_symbol_counts.items())),
        "final_selected_symbol_counts": dict(sorted(final_symbol_counts.items())),
        "excursion_tracking_status_counts": dict(sorted(excursion_status_counts.items())),
        "quote_read_failures_are_never_retried": True,
        "account_fee_read_failures_are_never_retried": True,
        "fallback_never_relaxes_entry_thresholds": True,
        "fallback_occurs_before_entry_ack_only": True,
        "fallback_selection_is_not_realized_profit": True,
        "explicit_writes_require_exchange_protection_reconciliation": True,
        "excursion_tracking_is_diagnostics_only": True,
        "strategy_promotion_allowed": False,
        "live_mainnet_order_routing_allowed": False,
    }


def _retryable_quote_block(result: BybitDemoStrategyCycleResult) -> bool:
    if result.status is not BybitDemoStrategyCycleStatus.PRE_ENTRY_QUOTE_BLOCKED:
        return False
    if not result.pre_entry_quote_reasons:
        return False
    return not any(
        reason.startswith("PRE_ENTRY_QUOTE_READ_FAILED")
        for reason in result.pre_entry_quote_reasons
    )


def _retryable_account_fee_reasons(
    result: BybitDemoStrategyCycleResult,
) -> tuple[str, ...] | None:
    if result.status is not BybitDemoStrategyCycleStatus.GUARDED_ORCHESTRATOR_CALLED:
        return None
    orchestrator = result.orchestrator_result
    if (
        orchestrator is None
        or orchestrator.status is not BybitDemoOrchestratorStatus.CYCLE_EXECUTED
    ):
        return None
    cycle = orchestrator.cycle_result
    if cycle is None or cycle.status is not BybitDemoCycleStatus.ENTRY_BLOCKED:
        return None
    if cycle.entry_ack is not None or not cycle.reasons:
        return None
    if not set(cycle.reasons).issubset(_ACCOUNT_FEE_RETRYABLE_REASONS):
        return None
    return cycle.reasons


def _exclude_instrument(
    instruments: dict[str, BybitInstrumentSpec],
    symbol: str,
) -> bool:
    return instruments.pop(symbol, None) is not None


def _ranked_result(
    cycle_result: BybitDemoStrategyCycleResult,
    attempts: Sequence[BybitDemoCandidateFallbackAttempt],
    *,
    candidates_exhausted: bool,
) -> BybitDemoRankedFallbackResult:
    selected_plan = cycle_result.selection.selected_trade_plan
    selected_after_fallback = bool(attempts) and (
        cycle_result.status is BybitDemoStrategyCycleStatus.GUARDED_ORCHESTRATOR_CALLED
        and selected_plan is not None
        and not candidates_exhausted
    )
    final_symbol = None if candidates_exhausted or selected_plan is None else selected_plan.symbol
    return BybitDemoRankedFallbackResult(
        cycle_result=cycle_result,
        fallback_attempts=tuple(attempts),
        selected_after_fallback=selected_after_fallback,
        candidates_exhausted=candidates_exhausted,
        final_selected_symbol=final_symbol,
        demo_only=True,
        strategy_promotion_allowed=False,
        live_mainnet_order_routing_allowed=False,
    )


def _initialize_optional_excursion_tracking(
    account_result: BybitDemoAccountSizedCycleResult,
    *,
    excursion_store: BybitDemoExcursionStore | None,
) -> BybitDemoExcursionRuntimeResult | None:
    if excursion_store is None:
        return None
    strategy_cycle = getattr(account_result, "strategy_cycle_result", None)
    if strategy_cycle is None:
        return None
    tracking = initialize_bybit_demo_excursion_from_strategy_cycle(
        strategy_cycle,
        store=excursion_store,
    )
    if tracking.live_mainnet_order_routing_allowed:
        raise ValueError("resilient demo cycle received live excursion tracking result")
    if tracking.status is BybitDemoExcursionRuntimeStatus.TRACKING_BLOCKED:
        return tracking
    return tracking


def _validate_optional_excursion_store(
    excursion_store: BybitDemoExcursionStore | None,
) -> None:
    if excursion_store is None:
        return
    if getattr(excursion_store, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("resilient demo cycle rejected mainnet-capable excursion store")
    if getattr(excursion_store, "order_writes_supported", True) is not False:
        raise ValueError("resilient demo cycle requires diagnostics-only excursion store")


def _reject_live_cycle(result: BybitDemoStrategyCycleResult) -> None:
    if result.live_mainnet_order_routing_allowed:
        raise ValueError("ranked fallback cycle received live mainnet permission")
