from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.execution.bybit_demo_controller import BybitDemoEntryPlan, plan_bybit_demo_entry
from app.execution.bybit_demo_cycle import BybitDemoCyclePolicy
from app.execution.bybit_demo_funding_reconciliation import BybitDemoFundingLedgerWindow
from app.execution.bybit_demo_lifecycle_gate import BybitDemoLifecyclePolicy
from app.execution.bybit_demo_orchestrator import (
    BybitDemoOrchestratorResult,
    BybitDemoPreviousTradeReference,
    execute_reconciled_guarded_bybit_demo_cycle,
)
from app.marketdata.bybit_demo_quotes import BybitDemoMarketQuoteClient
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.marketdata.bybit_v5 import BybitKlineBar
from app.strategy.crypto_correlation import (
    CryptoCorrelationPolicy,
    evaluate_crypto_correlation,
)
from app.strategy.crypto_execution_risk import (
    CryptoExecutionRiskPolicy,
    resize_trade_plan_at_next_open,
)
from app.strategy.crypto_perp import (
    CryptoPerpStrategyConfig,
    CryptoSide,
    CryptoTradePlan,
    build_trade_plan,
    rank_crypto_signals,
)
from app.strategy.crypto_position_selection import (
    CryptoPositionCandidate,
    rank_crypto_position_candidates,
)
from app.strategy.crypto_session_risk import (
    CryptoSessionRiskPolicy,
    CryptoSessionRiskState,
    evaluate_crypto_session_risk,
)


class BybitDemoStrategySelectionStatus(StrEnum):
    SELECTED = "SELECTED"
    SESSION_RISK_BLOCKED = "SESSION_RISK_BLOCKED"
    PORTFOLIO_STATE_BLOCKED = "PORTFOLIO_STATE_BLOCKED"
    PORTFOLIO_CONCURRENCY_BLOCKED = "PORTFOLIO_CONCURRENCY_BLOCKED"
    NO_EXECUTABLE_PLAN = "NO_EXECUTABLE_PLAN"


class BybitDemoStrategyCycleStatus(StrEnum):
    NO_TRADE = "NO_TRADE"
    PORTFOLIO_STATE_BLOCKED = "PORTFOLIO_STATE_BLOCKED"
    PRE_ENTRY_QUOTE_BLOCKED = "PRE_ENTRY_QUOTE_BLOCKED"
    GUARDED_ORCHESTRATOR_CALLED = "GUARDED_ORCHESTRATOR_CALLED"


@dataclass(frozen=True)
class BybitDemoStrategyCandidateAudit:
    signal_rank: int
    symbol: str
    signal_eligible: bool
    signal_reasons: tuple[str, ...]
    plan_eligible: bool
    plan_reasons: tuple[str, ...]
    demo_preflight_eligible: bool
    demo_preflight_reasons: tuple[str, ...]
    portfolio_reasons: tuple[str, ...] = ()
    correlation_blocking_symbol: str | None = None
    correlation: Decimal | None = None


@dataclass(frozen=True)
class BybitDemoStrategySelection:
    status: BybitDemoStrategySelectionStatus
    reasons: tuple[str, ...]
    selected_trade_plan: CryptoTradePlan | None
    selected_entry_preflight: BybitDemoEntryPlan | None
    selected_signal_rank: int | None
    candidate_audit: tuple[BybitDemoStrategyCandidateAudit, ...]
    executable_candidate_count: int
    economic_shadow_selected_symbol: str | None
    economic_shadow_selected_side: str | None
    economic_shadow_differs_from_current: bool
    open_position_symbols: tuple[str, ...] = ()
    correlation_block_count: int = 0
    portfolio_state_checked: bool = False
    correlation_shadow_only: bool = True
    correlation_demo_activation_allowed: bool = False
    economic_shadow_activation_allowed: bool = False
    order_write_performed: bool = False
    demo_only: bool = True
    strategy_promotion_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False


@dataclass(frozen=True)
class BybitDemoStrategyCycleResult:
    status: BybitDemoStrategyCycleStatus
    selection: BybitDemoStrategySelection
    orchestrator_result: BybitDemoOrchestratorResult | None
    pre_entry_quote_checked: bool = False
    pre_entry_quote_price: Decimal | None = None
    pre_entry_modeled_entry_price: Decimal | None = None
    pre_entry_quote_resized: bool = False
    pre_entry_original_quantity: Decimal | None = None
    pre_entry_adjusted_quantity: Decimal | None = None
    pre_entry_quote_reasons: tuple[str, ...] = ()
    demo_only: bool = True
    strategy_promotion_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False


Orchestrator = Callable[..., BybitDemoOrchestratorResult]
Sleeper = Callable[[float], None]


def select_bybit_demo_trade_plan(
    bars_by_symbol: Mapping[str, Sequence[BybitKlineBar]],
    *,
    instruments: Mapping[str, BybitInstrumentSpec],
    strategy_config: CryptoPerpStrategyConfig,
    session_state: CryptoSessionRiskState,
    now: datetime,
    interval_minutes: int = 5,
    session_policy: CryptoSessionRiskPolicy | None = None,
    correlation_policy: CryptoCorrelationPolicy | None = None,
    open_position_symbols: Sequence[str] = (),
    portfolio_state_checked: bool = False,
) -> BybitDemoStrategySelection:
    """Select one demo-ready plan from completed bars and current portfolio state."""

    strategy_config.validate()
    session_state.validate()
    _validate_completed_histories(
        bars_by_symbol,
        now=now,
        interval_minutes=interval_minutes,
    )
    open_symbols = _validated_open_position_symbols(open_position_symbols)
    if len(open_symbols) >= strategy_config.maximum_concurrent_positions:
        return _selection_without_plan(
            BybitDemoStrategySelectionStatus.PORTFOLIO_CONCURRENCY_BLOCKED,
            reasons=("MAXIMUM_CONCURRENT_POSITIONS_REACHED",),
            open_position_symbols=open_symbols,
            portfolio_state_checked=portfolio_state_checked,
        )

    risk = evaluate_crypto_session_risk(session_state, session_policy)
    if not risk.new_entries_allowed:
        return _selection_without_plan(
            BybitDemoStrategySelectionStatus.SESSION_RISK_BLOCKED,
            reasons=risk.reasons,
            open_position_symbols=open_symbols,
            portfolio_state_checked=portfolio_state_checked,
        )

    active_correlation = (
        CryptoCorrelationPolicy()
        if correlation_policy is None
        else correlation_policy
    )
    active_correlation.validate()
    rankings = rank_crypto_signals(dict(bars_by_symbol), strategy_config)
    audits: list[BybitDemoStrategyCandidateAudit] = []
    executable: list[tuple[int, CryptoPositionCandidate, BybitDemoEntryPlan]] = []
    correlation_block_count = 0
    for signal_rank, evaluation in enumerate(rankings, start=1):
        if evaluation.signal is None:
            audits.append(
                BybitDemoStrategyCandidateAudit(
                    signal_rank=signal_rank,
                    symbol=evaluation.symbol,
                    signal_eligible=False,
                    signal_reasons=evaluation.reasons,
                    plan_eligible=False,
                    plan_reasons=(),
                    demo_preflight_eligible=False,
                    demo_preflight_reasons=(),
                )
            )
            continue

        if evaluation.symbol in open_symbols:
            audits.append(
                BybitDemoStrategyCandidateAudit(
                    signal_rank=signal_rank,
                    symbol=evaluation.symbol,
                    signal_eligible=True,
                    signal_reasons=(),
                    plan_eligible=False,
                    plan_reasons=(),
                    demo_preflight_eligible=False,
                    demo_preflight_reasons=(),
                    portfolio_reasons=("PREEXISTING_SYMBOL_POSITION_EXCLUDED",),
                )
            )
            continue

        correlation = evaluate_crypto_correlation(
            evaluation.symbol,
            selected_symbols=open_symbols,
            histories=bars_by_symbol,
            policy=active_correlation,
        )
        correlation_reasons: tuple[str, ...] = ()
        correlation_blocking_symbol = None
        correlation_value = None
        if not correlation.eligible:
            correlation_block_count += 1
            correlation_reasons = (
                correlation.reason or "CORRELATION_DIVERSIFICATION_BLOCK",
            )
            correlation_blocking_symbol = correlation.blocking_symbol
            correlation_value = correlation.correlation

        plan_evaluation = build_trade_plan(
            evaluation.signal,
            equity_usdt=session_state.current_equity_usdt,
            config=strategy_config,
        )
        if not plan_evaluation.eligible or plan_evaluation.plan is None:
            audits.append(
                BybitDemoStrategyCandidateAudit(
                    signal_rank=signal_rank,
                    symbol=evaluation.symbol,
                    signal_eligible=True,
                    signal_reasons=(),
                    plan_eligible=False,
                    plan_reasons=plan_evaluation.reasons,
                    demo_preflight_eligible=False,
                    demo_preflight_reasons=(),
                    portfolio_reasons=correlation_reasons,
                    correlation_blocking_symbol=correlation_blocking_symbol,
                    correlation=correlation_value,
                )
            )
            continue

        plan = plan_evaluation.plan
        instrument = instruments.get(plan.symbol)
        if instrument is None:
            audits.append(
                BybitDemoStrategyCandidateAudit(
                    signal_rank=signal_rank,
                    symbol=evaluation.symbol,
                    signal_eligible=True,
                    signal_reasons=(),
                    plan_eligible=True,
                    plan_reasons=(),
                    demo_preflight_eligible=False,
                    demo_preflight_reasons=("BYBIT_INSTRUMENT_SPEC_UNAVAILABLE",),
                    portfolio_reasons=correlation_reasons,
                    correlation_blocking_symbol=correlation_blocking_symbol,
                    correlation=correlation_value,
                )
            )
            continue

        preflight = plan_bybit_demo_entry(
            plan,
            instrument=instrument,
            session_state=session_state,
            session_policy=session_policy,
        )
        audits.append(
            BybitDemoStrategyCandidateAudit(
                signal_rank=signal_rank,
                symbol=evaluation.symbol,
                signal_eligible=True,
                signal_reasons=(),
                plan_eligible=True,
                plan_reasons=(),
                demo_preflight_eligible=preflight.eligible,
                demo_preflight_reasons=preflight.reasons,
                portfolio_reasons=correlation_reasons,
                correlation_blocking_symbol=correlation_blocking_symbol,
                correlation=correlation_value,
            )
        )
        if preflight.eligible:
            executable.append(
                (
                    signal_rank,
                    CryptoPositionCandidate(signal=evaluation.signal, plan=plan),
                    preflight,
                )
            )

    if not executable:
        return _selection_without_plan(
            BybitDemoStrategySelectionStatus.NO_EXECUTABLE_PLAN,
            reasons=("NO_DEMO_EXECUTABLE_CRYPTO_PLAN",),
            candidate_audit=tuple(audits),
            open_position_symbols=open_symbols,
            correlation_block_count=correlation_block_count,
            portfolio_state_checked=portfolio_state_checked,
        )

    current_rank, current_candidate, current_preflight = min(
        executable,
        key=lambda item: item[0],
    )
    economic_ranked = rank_crypto_position_candidates(item[1] for item in executable)
    economic = economic_ranked[0]
    differs = (
        len(executable) >= 2
        and (
            economic.plan.symbol != current_candidate.plan.symbol
            or economic.plan.side is not current_candidate.plan.side
        )
    )
    return BybitDemoStrategySelection(
        status=BybitDemoStrategySelectionStatus.SELECTED,
        reasons=(),
        selected_trade_plan=current_candidate.plan,
        selected_entry_preflight=current_preflight,
        selected_signal_rank=current_rank,
        candidate_audit=tuple(audits),
        executable_candidate_count=len(executable),
        economic_shadow_selected_symbol=economic.plan.symbol,
        economic_shadow_selected_side=economic.plan.side.value,
        economic_shadow_differs_from_current=differs,
        open_position_symbols=open_symbols,
        correlation_block_count=correlation_block_count,
        portfolio_state_checked=portfolio_state_checked,
        correlation_shadow_only=True,
        correlation_demo_activation_allowed=False,
        economic_shadow_activation_allowed=False,
        order_write_performed=False,
        demo_only=True,
        strategy_promotion_allowed=False,
        live_mainnet_order_routing_allowed=False,
    )


def execute_selected_reconciled_guarded_bybit_demo_cycle(
    bars_by_symbol: Mapping[str, Sequence[BybitKlineBar]],
    *,
    instruments: Mapping[str, BybitInstrumentSpec],
    strategy_config: CryptoPerpStrategyConfig,
    session_state: CryptoSessionRiskState,
    now: datetime,
    client: Any,
    previous_trade: BybitDemoPreviousTradeReference | None = None,
    trade_read_client: Any | None = None,
    accounting_client: Any | None = None,
    funding_ledger: BybitDemoFundingLedgerWindow | None = None,
    lifecycle_policy: BybitDemoLifecyclePolicy | None = None,
    cycle_policy: BybitDemoCyclePolicy | None = None,
    session_policy: CryptoSessionRiskPolicy | None = None,
    correlation_policy: CryptoCorrelationPolicy | None = None,
    execution_risk_policy: CryptoExecutionRiskPolicy | None = None,
    quote_client: Any | None = None,
    interval_minutes: int = 5,
    sleeper: Sleeper = time.sleep,
    orchestrator: Orchestrator = execute_reconciled_guarded_bybit_demo_cycle,
) -> BybitDemoStrategyCycleResult:
    """Bridge completed-bar selection into the existing fail-closed demo orchestrator."""

    active_cycle_policy = BybitDemoCyclePolicy() if cycle_policy is None else cycle_policy
    active_cycle_policy.validate()
    open_symbols: tuple[str, ...] = ()
    portfolio_checked = False
    if active_cycle_policy.writes_enabled:
        if getattr(client, "live_mainnet_order_routing_allowed", False):
            raise ValueError("demo strategy selector rejected a mainnet-capable order client")
        portfolio_checked = True
        try:
            open_symbols = _open_position_symbols(client.get_positions())
        except Exception as exc:  # noqa: BLE001 - unresolved portfolio state must block writes.
            reasons = (f"DEMO_PORTFOLIO_READ_FAILED:{type(exc).__name__}",)
            selection = _selection_without_plan(
                BybitDemoStrategySelectionStatus.PORTFOLIO_STATE_BLOCKED,
                reasons=reasons,
                portfolio_state_checked=True,
            )
            return BybitDemoStrategyCycleResult(
                status=BybitDemoStrategyCycleStatus.PORTFOLIO_STATE_BLOCKED,
                selection=selection,
                orchestrator_result=None,
            )

    selection = select_bybit_demo_trade_plan(
        bars_by_symbol,
        instruments=instruments,
        strategy_config=strategy_config,
        session_state=session_state,
        now=now,
        interval_minutes=interval_minutes,
        session_policy=session_policy,
        correlation_policy=correlation_policy,
        open_position_symbols=open_symbols,
        portfolio_state_checked=portfolio_checked,
    )
    plan = selection.selected_trade_plan
    if plan is None:
        return BybitDemoStrategyCycleResult(
            status=BybitDemoStrategyCycleStatus.NO_TRADE,
            selection=selection,
            orchestrator_result=None,
        )
    instrument = instruments.get(plan.symbol)
    if instrument is None:
        raise AssertionError("selected demo strategy plan lost its instrument specification")

    quote_checked = False
    quote_price = None
    modeled_entry_price = None
    quote_resized = False
    original_quantity = None
    adjusted_quantity = None
    quote_reasons: tuple[str, ...] = ()
    final_plan = plan

    if active_cycle_policy.writes_enabled:
        quote_reader = BybitDemoMarketQuoteClient() if quote_client is None else quote_client
        if getattr(quote_reader, "live_mainnet_order_routing_allowed", False):
            raise ValueError("demo strategy quote guard rejected a mainnet-capable quote reader")
        quote_checked = True
        try:
            quote = quote_reader.get_quote(symbol=plan.symbol)
            quote.validate()
            if quote.symbol != plan.symbol:
                raise ValueError("Bybit demo executable quote symbol mismatch")
        except Exception as exc:  # noqa: BLE001 - missing executable quote must block any write.
            quote_reasons = (f"PRE_ENTRY_QUOTE_READ_FAILED:{type(exc).__name__}",)
            return BybitDemoStrategyCycleResult(
                status=BybitDemoStrategyCycleStatus.PRE_ENTRY_QUOTE_BLOCKED,
                selection=selection,
                orchestrator_result=None,
                pre_entry_quote_checked=True,
                pre_entry_quote_reasons=quote_reasons,
            )
        quote_price = quote.ask_price if plan.side is CryptoSide.LONG else quote.bid_price
        execution = resize_trade_plan_at_next_open(
            plan,
            raw_next_open_price=quote_price,
            strategy_config=strategy_config,
            policy=execution_risk_policy,
        )
        modeled_entry_price = execution.actual_entry_price
        original_quantity = execution.original_quantity
        adjusted_quantity = execution.adjusted_quantity
        quote_resized = execution.resized
        if not execution.eligible or execution.adjusted_plan is None:
            quote_reasons = execution.reasons
            return BybitDemoStrategyCycleResult(
                status=BybitDemoStrategyCycleStatus.PRE_ENTRY_QUOTE_BLOCKED,
                selection=selection,
                orchestrator_result=None,
                pre_entry_quote_checked=True,
                pre_entry_quote_price=quote_price,
                pre_entry_modeled_entry_price=modeled_entry_price,
                pre_entry_quote_resized=quote_resized,
                pre_entry_original_quantity=original_quantity,
                pre_entry_adjusted_quantity=adjusted_quantity,
                pre_entry_quote_reasons=quote_reasons,
            )

        quote_preflight = plan_bybit_demo_entry(
            execution.adjusted_plan,
            instrument=instrument,
            session_state=session_state,
            session_policy=session_policy,
        )
        if not quote_preflight.eligible or quote_preflight.order is None:
            quote_reasons = quote_preflight.reasons or ("PRE_ENTRY_QUOTE_INSTRUMENT_REJECTED",)
            return BybitDemoStrategyCycleResult(
                status=BybitDemoStrategyCycleStatus.PRE_ENTRY_QUOTE_BLOCKED,
                selection=selection,
                orchestrator_result=None,
                pre_entry_quote_checked=True,
                pre_entry_quote_price=quote_price,
                pre_entry_modeled_entry_price=modeled_entry_price,
                pre_entry_quote_resized=quote_resized,
                pre_entry_original_quantity=original_quantity,
                pre_entry_adjusted_quantity=adjusted_quantity,
                pre_entry_quote_reasons=quote_reasons,
            )

        quantized_plan = replace(
            execution.adjusted_plan,
            reference_quantity=quote_preflight.order.quantity,
        )
        final_execution = resize_trade_plan_at_next_open(
            quantized_plan,
            raw_next_open_price=quote_price,
            strategy_config=strategy_config,
            policy=execution_risk_policy,
        )
        modeled_entry_price = final_execution.actual_entry_price
        adjusted_quantity = final_execution.adjusted_quantity
        quote_resized = adjusted_quantity < plan.reference_quantity
        if not final_execution.eligible or final_execution.adjusted_plan is None:
            quote_reasons = final_execution.reasons
            return BybitDemoStrategyCycleResult(
                status=BybitDemoStrategyCycleStatus.PRE_ENTRY_QUOTE_BLOCKED,
                selection=selection,
                orchestrator_result=None,
                pre_entry_quote_checked=True,
                pre_entry_quote_price=quote_price,
                pre_entry_modeled_entry_price=modeled_entry_price,
                pre_entry_quote_resized=quote_resized,
                pre_entry_original_quantity=original_quantity,
                pre_entry_adjusted_quantity=adjusted_quantity,
                pre_entry_quote_reasons=quote_reasons,
            )
        final_plan = final_execution.adjusted_plan
        final_preflight = plan_bybit_demo_entry(
            final_plan,
            instrument=instrument,
            session_state=session_state,
            session_policy=session_policy,
        )
        if not final_preflight.eligible or final_preflight.order is None:
            quote_reasons = final_preflight.reasons or ("PRE_ENTRY_QUOTE_INSTRUMENT_REJECTED",)
            return BybitDemoStrategyCycleResult(
                status=BybitDemoStrategyCycleStatus.PRE_ENTRY_QUOTE_BLOCKED,
                selection=selection,
                orchestrator_result=None,
                pre_entry_quote_checked=True,
                pre_entry_quote_price=quote_price,
                pre_entry_modeled_entry_price=modeled_entry_price,
                pre_entry_quote_resized=quote_resized,
                pre_entry_original_quantity=original_quantity,
                pre_entry_adjusted_quantity=adjusted_quantity,
                pre_entry_quote_reasons=quote_reasons,
            )
        selection = replace(
            selection,
            selected_trade_plan=final_plan,
            selected_entry_preflight=final_preflight,
        )

    result = orchestrator(
        final_plan,
        instrument=instrument,
        strategy_config=strategy_config,
        session_state=session_state,
        client=client,
        previous_trade=previous_trade,
        trade_read_client=trade_read_client,
        accounting_client=accounting_client,
        funding_ledger=funding_ledger,
        lifecycle_policy=lifecycle_policy,
        cycle_policy=active_cycle_policy,
        session_policy=session_policy,
        sleeper=sleeper,
    )
    if result.live_mainnet_order_routing_allowed:
        raise ValueError("demo strategy orchestrator returned live mainnet permission")
    return BybitDemoStrategyCycleResult(
        status=BybitDemoStrategyCycleStatus.GUARDED_ORCHESTRATOR_CALLED,
        selection=selection,
        orchestrator_result=result,
        pre_entry_quote_checked=quote_checked,
        pre_entry_quote_price=quote_price,
        pre_entry_modeled_entry_price=modeled_entry_price,
        pre_entry_quote_resized=quote_resized,
        pre_entry_original_quantity=original_quantity,
        pre_entry_adjusted_quantity=adjusted_quantity,
        pre_entry_quote_reasons=quote_reasons,
        demo_only=True,
        strategy_promotion_allowed=False,
        live_mainnet_order_routing_allowed=False,
    )


def _selection_without_plan(
    status: BybitDemoStrategySelectionStatus,
    *,
    reasons: tuple[str, ...],
    candidate_audit: tuple[BybitDemoStrategyCandidateAudit, ...] = (),
    open_position_symbols: tuple[str, ...] = (),
    correlation_block_count: int = 0,
    portfolio_state_checked: bool = False,
) -> BybitDemoStrategySelection:
    return BybitDemoStrategySelection(
        status=status,
        reasons=reasons,
        selected_trade_plan=None,
        selected_entry_preflight=None,
        selected_signal_rank=None,
        candidate_audit=candidate_audit,
        executable_candidate_count=0,
        economic_shadow_selected_symbol=None,
        economic_shadow_selected_side=None,
        economic_shadow_differs_from_current=False,
        open_position_symbols=open_position_symbols,
        correlation_block_count=correlation_block_count,
        portfolio_state_checked=portfolio_state_checked,
        correlation_shadow_only=True,
        correlation_demo_activation_allowed=False,
    )


def _validated_open_position_symbols(symbols: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(symbols)
    if len(set(normalized)) != len(normalized):
        raise ValueError("Bybit demo open position symbols must be unique")
    if any(
        symbol != symbol.strip().upper() or not symbol.endswith("USDT")
        for symbol in normalized
    ):
        raise ValueError("Bybit demo open position symbols must be normalized USDT symbols")
    return tuple(sorted(normalized))


def _open_position_symbols(positions: Sequence[Any]) -> tuple[str, ...]:
    open_symbols: list[str] = []
    for position in positions:
        symbol = getattr(position, "symbol", None)
        size = getattr(position, "size", None)
        if not isinstance(symbol, str) or not isinstance(size, Decimal):
            raise ValueError("Bybit demo portfolio position has invalid symbol/size")
        if not size.is_finite() or size < 0:
            raise ValueError("Bybit demo portfolio position size must be finite and non-negative")
        if size == 0:
            continue
        open_symbols.append(symbol)
    return _validated_open_position_symbols(open_symbols)


def _validate_completed_histories(
    bars_by_symbol: Mapping[str, Sequence[BybitKlineBar]],
    *,
    now: datetime,
    interval_minutes: int,
) -> None:
    if len(bars_by_symbol) < 2:
        raise ValueError("Bybit demo strategy selection requires at least two symbols")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Bybit demo strategy selection requires timezone-aware now")
    if interval_minutes < 1:
        raise ValueError("Bybit demo strategy interval must be positive")
    latest_times: set[datetime] = set()
    cutoff = now.astimezone(UTC)
    interval = timedelta(minutes=interval_minutes)
    for symbol, bars in bars_by_symbol.items():
        if symbol != symbol.strip().upper() or not symbol:
            raise ValueError("Bybit demo strategy symbol keys must be normalized uppercase")
        if not bars:
            raise ValueError("Bybit demo strategy histories cannot be empty")
        previous: datetime | None = None
        for bar in bars:
            bar.validate()
            if bar.symbol != symbol:
                raise ValueError("Bybit demo strategy history symbol mismatch")
            if previous is not None and bar.start_time <= previous:
                raise ValueError("Bybit demo strategy histories must be strictly chronological")
            previous = bar.start_time
        latest = bars[-1].start_time.astimezone(UTC)
        if latest + interval > cutoff:
            raise ValueError("Bybit demo strategy selection rejected an incomplete latest bar")
        latest_times.add(latest)
    if len(latest_times) != 1:
        raise ValueError("Bybit demo strategy histories must share one completed decision bar")
