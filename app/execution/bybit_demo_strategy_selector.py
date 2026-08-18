from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.marketdata.bybit_v5 import BybitKlineBar
from app.strategy.crypto_perp import (
    CryptoPerpStrategyConfig,
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
    NO_EXECUTABLE_PLAN = "NO_EXECUTABLE_PLAN"


class BybitDemoStrategyCycleStatus(StrEnum):
    NO_TRADE = "NO_TRADE"
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
) -> BybitDemoStrategySelection:
    """Select one demo-ready trade plan from synchronized completed bars without writing orders."""

    strategy_config.validate()
    session_state.validate()
    _validate_completed_histories(
        bars_by_symbol,
        now=now,
        interval_minutes=interval_minutes,
    )
    risk = evaluate_crypto_session_risk(session_state, session_policy)
    if not risk.new_entries_allowed:
        return BybitDemoStrategySelection(
            status=BybitDemoStrategySelectionStatus.SESSION_RISK_BLOCKED,
            reasons=risk.reasons,
            selected_trade_plan=None,
            selected_entry_preflight=None,
            selected_signal_rank=None,
            candidate_audit=(),
            executable_candidate_count=0,
            economic_shadow_selected_symbol=None,
            economic_shadow_selected_side=None,
            economic_shadow_differs_from_current=False,
        )

    rankings = rank_crypto_signals(dict(bars_by_symbol), strategy_config)
    audits: list[BybitDemoStrategyCandidateAudit] = []
    executable: list[tuple[int, CryptoPositionCandidate, BybitDemoEntryPlan]] = []
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
        return BybitDemoStrategySelection(
            status=BybitDemoStrategySelectionStatus.NO_EXECUTABLE_PLAN,
            reasons=("NO_DEMO_EXECUTABLE_CRYPTO_PLAN",),
            selected_trade_plan=None,
            selected_entry_preflight=None,
            selected_signal_rank=None,
            candidate_audit=tuple(audits),
            executable_candidate_count=0,
            economic_shadow_selected_symbol=None,
            economic_shadow_selected_side=None,
            economic_shadow_differs_from_current=False,
        )

    current_rank, current_candidate, current_preflight = min(
        executable,
        key=lambda item: item[0],
    )
    economic_ranked = rank_crypto_position_candidates(
        item[1] for item in executable
    )
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
    interval_minutes: int = 5,
    sleeper: Sleeper = time.sleep,
    orchestrator: Orchestrator = execute_reconciled_guarded_bybit_demo_cycle,
) -> BybitDemoStrategyCycleResult:
    """Bridge completed-bar selection into the existing fail-closed demo orchestrator."""

    selection = select_bybit_demo_trade_plan(
        bars_by_symbol,
        instruments=instruments,
        strategy_config=strategy_config,
        session_state=session_state,
        now=now,
        interval_minutes=interval_minutes,
        session_policy=session_policy,
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
    result = orchestrator(
        plan,
        instrument=instrument,
        strategy_config=strategy_config,
        session_state=session_state,
        client=client,
        previous_trade=previous_trade,
        trade_read_client=trade_read_client,
        accounting_client=accounting_client,
        funding_ledger=funding_ledger,
        lifecycle_policy=lifecycle_policy,
        cycle_policy=cycle_policy,
        session_policy=session_policy,
        sleeper=sleeper,
    )
    if result.live_mainnet_order_routing_allowed:
        raise ValueError("demo strategy orchestrator returned live mainnet permission")
    return BybitDemoStrategyCycleResult(
        status=BybitDemoStrategyCycleStatus.GUARDED_ORCHESTRATOR_CALLED,
        selection=selection,
        orchestrator_result=result,
        demo_only=True,
        strategy_promotion_allowed=False,
        live_mainnet_order_routing_allowed=False,
    )


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
