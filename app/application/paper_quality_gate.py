from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.application.paper_execution_quality import (
    PaperExecutionQualityFill,
    SQLitePaperExecutionQualityStore,
)
from app.application.paper_reaction_quality import (
    PaperReactionFill,
    SQLitePaperReactionQualityStore,
)
from app.strategy.quality_monitor import StrategyQualityGateDecision


class PaperQualityGateStatus(StrEnum):
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    HEALTHY = "HEALTHY"
    PAUSE_ENTRIES = "PAUSE_ENTRIES"


@dataclass(frozen=True)
class ExecutionQualityGatePolicy:
    window_fills: int
    minimum_observations: int
    maximum_weighted_signed_slippage_bps: Decimal
    maximum_worst_signed_slippage_bps: Decimal
    allow_entries_when_insufficient_data: bool = False

    def validate(self) -> None:
        if self.window_fills < 1:
            raise ValueError("execution window_fills must be positive")
        if self.minimum_observations < 1:
            raise ValueError("execution minimum_observations must be positive")
        if self.minimum_observations > self.window_fills:
            raise ValueError(
                "execution minimum_observations cannot exceed window_fills"
            )
        for field_name, value in (
            (
                "maximum_weighted_signed_slippage_bps",
                self.maximum_weighted_signed_slippage_bps,
            ),
            (
                "maximum_worst_signed_slippage_bps",
                self.maximum_worst_signed_slippage_bps,
            ),
        ):
            if not value.is_finite() or value < 0:
                raise ValueError(f"{field_name} must be finite and non-negative")


@dataclass(frozen=True)
class ReactionQualityGatePolicy:
    window_fills: int
    minimum_observations: int
    maximum_average_latency_seconds: Decimal
    maximum_p95_latency_seconds: Decimal
    allow_entries_when_insufficient_data: bool = False

    def validate(self) -> None:
        if self.window_fills < 1:
            raise ValueError("reaction window_fills must be positive")
        if self.minimum_observations < 1:
            raise ValueError("reaction minimum_observations must be positive")
        if self.minimum_observations > self.window_fills:
            raise ValueError(
                "reaction minimum_observations cannot exceed window_fills"
            )
        for field_name, value in (
            ("maximum_average_latency_seconds", self.maximum_average_latency_seconds),
            ("maximum_p95_latency_seconds", self.maximum_p95_latency_seconds),
        ):
            if not value.is_finite() or value < 0:
                raise ValueError(f"{field_name} must be finite and non-negative")


@dataclass(frozen=True)
class ExecutionQualityGateMetrics:
    observation_count: int
    adverse_fill_count: int
    expected_notional: Decimal
    signed_slippage_notional: Decimal
    weighted_signed_slippage_bps: Decimal | None
    worst_signed_slippage_bps: Decimal | None


@dataclass(frozen=True)
class ReactionQualityGateMetrics:
    observation_count: int
    average_latency_seconds: Decimal | None
    p95_latency_seconds: Decimal | None
    maximum_latency_seconds: Decimal | None


@dataclass(frozen=True)
class ComponentGateDecision:
    sufficient_data: bool
    allow_new_entries: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PaperQualityGateDecision:
    status: PaperQualityGateStatus
    allow_new_entries: bool
    allow_exits: bool
    reasons: tuple[str, ...]
    trade_gate: StrategyQualityGateDecision
    execution: ExecutionQualityGateMetrics | None
    reaction: ReactionQualityGateMetrics | None


def evaluate_paper_quality_gate(
    *,
    trade_gate: StrategyQualityGateDecision,
    execution_store: SQLitePaperExecutionQualityStore | None = None,
    execution_policy: ExecutionQualityGatePolicy | None = None,
    reaction_store: SQLitePaperReactionQualityStore | None = None,
    reaction_policy: ReactionQualityGatePolicy | None = None,
    strategy_id: str | None = None,
) -> PaperQualityGateDecision:
    """Compose paper trade, execution and reaction health into one entry gate.

    The returned object implements the ``EntryExitGate`` protocol consumed by the
    portfolio paper planner. It may pause new BUYs, but it never blocks SELLs/exits.
    Thresholds are deliberately explicit inputs rather than hidden production defaults.
    """

    if not trade_gate.allow_exits:
        raise ValueError("trade quality gate must never block exits")
    if (execution_store is None) != (execution_policy is None):
        raise ValueError("execution store and policy must be supplied together")
    if (reaction_store is None) != (reaction_policy is None):
        raise ValueError("reaction store and policy must be supplied together")
    if reaction_store is not None and (strategy_id is None or not strategy_id.strip()):
        raise ValueError("strategy_id is required for reaction quality")

    execution_metrics: ExecutionQualityGateMetrics | None = None
    execution_decision: ComponentGateDecision | None = None
    if execution_store is not None and execution_policy is not None:
        execution_policy.validate()
        execution_metrics, execution_decision = _execution_gate(
            execution_store,
            policy=execution_policy,
        )

    reaction_metrics: ReactionQualityGateMetrics | None = None
    reaction_decision: ComponentGateDecision | None = None
    if reaction_store is not None and reaction_policy is not None:
        reaction_policy.validate()
        reaction_metrics, reaction_decision = _reaction_gate(
            reaction_store,
            strategy_id=strategy_id or "",
            policy=reaction_policy,
        )

    reasons: list[str] = []
    if not trade_gate.allow_new_entries:
        reasons.extend(f"TRADE:{reason}" for reason in trade_gate.reasons)
    if execution_decision is not None and not execution_decision.allow_new_entries:
        reasons.extend(execution_decision.reasons)
    if reaction_decision is not None and not reaction_decision.allow_new_entries:
        reasons.extend(reaction_decision.reasons)

    allow_new_entries = (
        trade_gate.allow_new_entries
        and (
            execution_decision is None
            or execution_decision.allow_new_entries
        )
        and (
            reaction_decision is None
            or reaction_decision.allow_new_entries
        )
    )
    insufficient = (
        trade_gate.status.value == PaperQualityGateStatus.INSUFFICIENT_DATA.value
        or (
            execution_decision is not None
            and not execution_decision.sufficient_data
        )
        or (
            reaction_decision is not None
            and not reaction_decision.sufficient_data
        )
    )
    degraded = any(
        reason
        for reason in reasons
        if "INSUFFICIENT_OBSERVATIONS" not in reason
    )
    status = (
        PaperQualityGateStatus.PAUSE_ENTRIES
        if degraded
        else (
            PaperQualityGateStatus.INSUFFICIENT_DATA
            if insufficient
            else PaperQualityGateStatus.HEALTHY
        )
    )
    return PaperQualityGateDecision(
        status=status,
        allow_new_entries=allow_new_entries,
        allow_exits=True,
        reasons=tuple(dict.fromkeys(reasons)),
        trade_gate=trade_gate,
        execution=execution_metrics,
        reaction=reaction_metrics,
    )


def _execution_gate(
    store: SQLitePaperExecutionQualityStore,
    *,
    policy: ExecutionQualityGatePolicy,
) -> tuple[ExecutionQualityGateMetrics, ComponentGateDecision]:
    fills = store.fills()[-policy.window_fills :]
    expected_notional = sum(
        (item.expected_limit_price * item.quantity for item in fills),
        start=Decimal("0"),
    )
    signed_notional = sum(
        (item.signed_slippage_notional for item in fills),
        start=Decimal("0"),
    )
    weighted_bps = (
        None
        if expected_notional == 0
        else signed_notional / expected_notional * Decimal("10000")
    )
    worst_bps = (
        None
        if not fills
        else max(item.signed_slippage_bps for item in fills)
    )
    metrics = ExecutionQualityGateMetrics(
        observation_count=len(fills),
        adverse_fill_count=sum(item.signed_slippage_fraction > 0 for item in fills),
        expected_notional=expected_notional,
        signed_slippage_notional=signed_notional,
        weighted_signed_slippage_bps=weighted_bps,
        worst_signed_slippage_bps=worst_bps,
    )
    if len(fills) < policy.minimum_observations:
        return metrics, ComponentGateDecision(
            sufficient_data=False,
            allow_new_entries=policy.allow_entries_when_insufficient_data,
            reasons=("EXECUTION:INSUFFICIENT_OBSERVATIONS",),
        )

    reasons: list[str] = []
    if (
        weighted_bps is not None
        and weighted_bps > policy.maximum_weighted_signed_slippage_bps
    ):
        reasons.append("EXECUTION:WEIGHTED_SLIPPAGE_ABOVE_MAXIMUM")
    if (
        worst_bps is not None
        and worst_bps > policy.maximum_worst_signed_slippage_bps
    ):
        reasons.append("EXECUTION:WORST_SLIPPAGE_ABOVE_MAXIMUM")
    return metrics, ComponentGateDecision(
        sufficient_data=True,
        allow_new_entries=not reasons,
        reasons=tuple(reasons),
    )


def _reaction_gate(
    store: SQLitePaperReactionQualityStore,
    *,
    strategy_id: str,
    policy: ReactionQualityGatePolicy,
) -> tuple[ReactionQualityGateMetrics, ComponentGateDecision]:
    fills = store.fills(strategy_id=strategy_id)[-policy.window_fills :]
    latencies = tuple(sorted(item.latency_seconds for item in fills))
    average = (
        None
        if not latencies
        else sum(latencies, start=Decimal("0")) / Decimal(len(latencies))
    )
    p95 = None if not latencies else latencies[_p95_index(len(latencies))]
    maximum = None if not latencies else latencies[-1]
    metrics = ReactionQualityGateMetrics(
        observation_count=len(fills),
        average_latency_seconds=average,
        p95_latency_seconds=p95,
        maximum_latency_seconds=maximum,
    )
    if len(fills) < policy.minimum_observations:
        return metrics, ComponentGateDecision(
            sufficient_data=False,
            allow_new_entries=policy.allow_entries_when_insufficient_data,
            reasons=("REACTION:INSUFFICIENT_OBSERVATIONS",),
        )

    reasons: list[str] = []
    if average is not None and average > policy.maximum_average_latency_seconds:
        reasons.append("REACTION:AVERAGE_LATENCY_ABOVE_MAXIMUM")
    if p95 is not None and p95 > policy.maximum_p95_latency_seconds:
        reasons.append("REACTION:P95_LATENCY_ABOVE_MAXIMUM")
    return metrics, ComponentGateDecision(
        sufficient_data=True,
        allow_new_entries=not reasons,
        reasons=tuple(reasons),
    )


def _p95_index(count: int) -> int:
    if count < 1:
        raise ValueError("p95 requires at least one observation")
    return max(0, (95 * count + 99) // 100 - 1)
