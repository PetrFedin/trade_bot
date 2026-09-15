from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.application.paper_cycle import PaperPlanningResult
from app.domain.trading import Bar
from app.marketdata.continuity import OperationalContinuityStore
from app.marketdata.decision_leases import (
    DecisionLeasePolicy,
    DecisionLeaseReceipt,
    DecisionLeaseStore,
    DecisionSafetyEvidence,
    decision_safety_evidence_id,
)
from app.marketdata.operational import OperationalBar, OperationalMarketDataStore
from app.observability.readiness import (
    OperationalReadiness,
    OperationalReadinessEvaluator,
    OperationalSnapshot,
)
from app.risk.pretrade import OperationalRiskContext
from app.runtime.paper_dispatch_control import (
    DispatchControlMode,
    PaperDispatchControlStore,
)

OperationalSnapshotProvider = Callable[[], OperationalSnapshot]


class OperationalRiskContextProvider(Protocol):
    def context_for_decision(
        self,
        *,
        receipt: DecisionLeaseReceipt,
        bars: Sequence[Bar],
        decision_time: datetime,
    ) -> OperationalRiskContext: ...


class OperationalDecisionPlanner(Protocol):
    def plan_and_prepare(
        self,
        bars: Sequence[Bar],
        *,
        decision_time: datetime,
        risk_context: OperationalRiskContext,
        kill_switch_engaged: bool = False,
    ) -> PaperPlanningResult: ...


@dataclass(frozen=True)
class OperationalDecisionWorkerPolicy:
    required_bars: int = 3
    lease: DecisionLeasePolicy = DecisionLeasePolicy()

    def validate(self) -> None:
        if self.required_bars < 3:
            raise ValueError("required_bars must be at least three")
        self.lease.validate()


@dataclass(frozen=True)
class OperationalDecisionWorkerResult:
    receipt: DecisionLeaseReceipt
    status: str
    safety: DecisionSafetyEvidence
    outcome_id: str | None
    planning: PaperPlanningResult | None

    def validate(self) -> None:
        self.receipt.validate()
        self.safety.validate()
        if self.status not in {"BLOCKED", "COMPLETED"}:
            raise ValueError("unknown operational decision worker status")
        if self.status == "BLOCKED":
            if self.safety.ready_for_evaluation:
                raise ValueError("blocked decision requires safety reasons")
            if self.outcome_id is not None or self.planning is not None:
                raise ValueError("blocked decision cannot contain planning outcome")
        if self.status == "COMPLETED":
            if not self.safety.ready_for_evaluation:
                raise ValueError("completed decision requires ready safety evidence")
            if self.outcome_id is None or self.planning is None:
                raise ValueError("completed decision requires planning outcome")


class OperationalDecisionWorker:
    """Lease one durable decision, prove current safety, then invoke the stable planner."""

    def __init__(
        self,
        *,
        strategy_id: str,
        owner_id: str,
        release_identity: str,
        leases: DecisionLeaseStore,
        marketdata: OperationalMarketDataStore,
        continuity: OperationalContinuityStore,
        readiness: OperationalReadinessEvaluator,
        snapshot_provider: OperationalSnapshotProvider | None,
        control: PaperDispatchControlStore,
        risk_context_provider: OperationalRiskContextProvider,
        planner: OperationalDecisionPlanner,
        policy: OperationalDecisionWorkerPolicy | None = None,
    ) -> None:
        if not strategy_id.strip() or not owner_id.strip() or not release_identity.strip():
            raise ValueError("strategy/owner/release identity is required")
        if risk_context_provider is None or planner is None:
            raise ValueError("risk_context_provider and planner are required")
        resolved = OperationalDecisionWorkerPolicy() if policy is None else policy
        resolved.validate()
        self.strategy_id = strategy_id
        self.owner_id = owner_id
        self.release_identity = release_identity
        self.leases = leases
        self.marketdata = marketdata
        self.continuity = continuity
        self.readiness = readiness
        self.snapshot_provider = snapshot_provider
        self.control = control
        self.risk_context_provider = risk_context_provider
        self.planner = planner
        self.policy = resolved

    def run_next(self, *, occurred_at: datetime) -> OperationalDecisionWorkerResult | None:
        moment = _aware(occurred_at, "occurred_at")
        receipt = self.leases.claim_next(
            strategy_id=self.strategy_id,
            owner_id=self.owner_id,
            release_identity=self.release_identity,
            occurred_at=moment,
            policy=self.policy.lease,
        )
        if receipt is None:
            return None

        operational_bars, continuity_reasons, checkpoint_id = self._decision_window(receipt)
        readiness, readiness_reasons, snapshot = self._readiness()
        control_state = self.control.current()
        first_bar_id = None if not operational_bars else operational_bars[0].bar_id
        last_bar_id = None if not operational_bars else operational_bars[-1].bar_id
        evidence = DecisionSafetyEvidence(
            evidence_id=decision_safety_evidence_id(
                ticket_id=receipt.ticket.ticket_id,
                owner_id=receipt.owner_id,
                release_identity=receipt.release_identity,
                fencing_token=receipt.fencing_token,
                checkpoint_id=checkpoint_id,
                first_bar_id=first_bar_id,
                last_bar_id=last_bar_id,
                continuity_reasons=continuity_reasons,
                readiness_reasons=readiness_reasons,
                control_mode=control_state.mode.value,
                control_version=control_state.version,
                observed_at=moment,
            ),
            ticket_id=receipt.ticket.ticket_id,
            owner_id=receipt.owner_id,
            release_identity=receipt.release_identity,
            fencing_token=receipt.fencing_token,
            checkpoint_id=checkpoint_id,
            first_bar_id=first_bar_id,
            last_bar_id=last_bar_id,
            continuity_reasons=continuity_reasons,
            readiness_reasons=readiness_reasons,
            control_mode=control_state.mode.value,
            control_version=control_state.version,
            observed_at=moment,
        )
        self.leases.record_safety(evidence)

        if not evidence.ready_for_evaluation:
            self._halt_if_armed(evidence, occurred_at=moment)
            self.leases.release(receipt, occurred_at=moment)
            result = OperationalDecisionWorkerResult(
                receipt=receipt,
                status="BLOCKED",
                safety=evidence,
                outcome_id=None,
                planning=None,
            )
            result.validate()
            return result

        domain_bars = tuple(_strategy_bar(bar) for bar in operational_bars)
        risk_context = self.risk_context_provider.context_for_decision(
            receipt=receipt,
            bars=domain_bars,
            decision_time=moment,
        )
        renewed = self.leases.renew(
            receipt,
            occurred_at=moment,
            policy=self.policy.lease,
        )
        planning = self.planner.plan_and_prepare(
            domain_bars,
            decision_time=moment,
            risk_context=risk_context,
            kill_switch_engaged=False if snapshot is None else snapshot.kill_switch_engaged,
        )
        outcome_id = _planning_outcome_id(planning)
        self.leases.complete(
            renewed,
            outcome_id=outcome_id,
            occurred_at=moment,
        )
        result = OperationalDecisionWorkerResult(
            receipt=renewed,
            status="COMPLETED",
            safety=evidence,
            outcome_id=outcome_id,
            planning=planning,
        )
        result.validate()
        return result

    def _decision_window(
        self,
        receipt: DecisionLeaseReceipt,
    ) -> tuple[tuple[OperationalBar, ...], tuple[str, ...], str | None]:
        reasons: set[str] = set()
        checkpoint = self.continuity.latest(
            provider=receipt.provider,
            venue=receipt.venue,
            symbol=receipt.symbol,
            interval_seconds=receipt.interval_seconds,
        )
        checkpoint_id = None if checkpoint is None else checkpoint.checkpoint_id
        if checkpoint is None:
            reasons.add("CONTINUITY_CHECKPOINT_REQUIRED")
        elif checkpoint.through_close_time < receipt.bar_close_time:
            reasons.add("CONTINUITY_BEHIND_DECISION_BAR")
        else:
            checkpoint_tail = self.marketdata.recent_bars(
                provider=receipt.provider,
                venue=receipt.venue,
                symbol=receipt.symbol,
                interval_seconds=receipt.interval_seconds,
                through_close_time=checkpoint.through_close_time,
                limit=1,
            )
            if not checkpoint_tail or checkpoint_tail[-1].bar_id != checkpoint.through_bar_id:
                reasons.add("CONTINUITY_HIGH_WATER_CONFLICTED_OR_MISSING")

        bars = self.marketdata.recent_bars(
            provider=receipt.provider,
            venue=receipt.venue,
            symbol=receipt.symbol,
            interval_seconds=receipt.interval_seconds,
            through_close_time=receipt.bar_close_time,
            limit=self.policy.required_bars,
        )
        if len(bars) != self.policy.required_bars:
            reasons.add("DECISION_WINDOW_INCOMPLETE")
        if bars and bars[-1].bar_id != receipt.ticket.bar_id:
            reasons.add("DECISION_BAR_NOT_WINDOW_TAIL")
        if len(bars) > 1:
            for previous, current in zip(bars, bars[1:], strict=True):
                if previous.close_time != current.open_time:
                    reasons.add("DECISION_WINDOW_GAP")
                    break
        return bars, tuple(sorted(reasons)), checkpoint_id

    def _readiness(
        self,
    ) -> tuple[OperationalReadiness | None, tuple[str, ...], OperationalSnapshot | None]:
        if self.snapshot_provider is None:
            return None, ("OPERATIONAL_SNAPSHOT_REQUIRED",), None
        try:
            snapshot = self.snapshot_provider()
            result = self.readiness.evaluate(snapshot)
        except Exception:
            return None, ("OPERATIONAL_SNAPSHOT_INVALID",), None
        reasons = () if result.ready_for_paper_operation else (
            result.reasons or ("OPERATIONAL_READINESS_FAILED",)
        )
        return result, tuple(sorted(set(reasons))), snapshot

    def _halt_if_armed(
        self,
        evidence: DecisionSafetyEvidence,
        *,
        occurred_at: datetime,
    ) -> None:
        current = self.control.current()
        if current.mode is not DispatchControlMode.ARMED:
            return
        reasons = tuple(sorted(set((*evidence.continuity_reasons, *evidence.readiness_reasons))))
        if not reasons:
            return
        self.control.halt(
            operator_id="system:decision-worker",
            reason="DECISION_SAFETY_FAILED:" + ",".join(reasons),
            occurred_at=occurred_at,
        )


def _strategy_bar(value: OperationalBar) -> Bar:
    bar = Bar(
        symbol=value.symbol,
        timestamp=value.close_time,
        close=value.close,
    )
    bar.validate()
    return bar


def _planning_outcome_id(result: PaperPlanningResult) -> str:
    material = {
        "strategy_id": result.target.strategy_id,
        "symbol": result.target.symbol,
        "generated_at": result.target.generated_at.isoformat(),
        "target_quantity": str(result.target.quantity),
        "intent_id": None if result.intent is None else result.intent.intent_id,
        "risk_approved": None if result.risk is None else result.risk.approved,
        "risk_reasons": [] if result.risk is None else list(result.risk.reasons),
        "prepared_intent_id": None
        if result.prepared is None
        else result.prepared.order.intent_id,
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return "planning:" + hashlib.sha256(encoded.encode()).hexdigest()


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value
