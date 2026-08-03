from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
import json
import os
from pathlib import Path
import threading
from typing import Mapping

from app.runtime.paper_broker_contract_v99 import (
    BrokerMutationError,
    BrokerOrder,
    BrokerOrderStatus,
    OrderSide,
    PaperBrokerV99,
)
from app.runtime.platform_common_v90 import canonical_json, require_aware, sha256_digest

_ZERO_DIGEST = "0" * 64


class RoundTripError(RuntimeError):
    pass


class JournalCorruption(RoundTripError):
    pass


class StaleGeneration(RoundTripError):
    pass


class RoundTripState(str, Enum):
    CREATED = "CREATED"
    PREFLIGHT = "PREFLIGHT"
    SUBMITTED = "SUBMITTED"
    REPLACED = "REPLACED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"
    RECOVERING = "RECOVERING"
    BLOCKED = "BLOCKED"


class RoundTripOutcome(str, Enum):
    CANCELLED_CLEAN = "CANCELLED_CLEAN"
    BROKER_REJECTED = "BROKER_REJECTED"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    RESIDUAL_PAPER_EXPOSURE = "RESIDUAL_PAPER_EXPOSURE"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class RoundTripPolicyV99:
    maximum_plan_age: timedelta = timedelta(minutes=10)
    maximum_evidence_age: timedelta = timedelta(seconds=45)
    maximum_quantity: Decimal = Decimal("1")
    maximum_notional: Decimal = Decimal("1000")
    required_account_status: str = "ACTIVE"
    required_currency: str = "USD"
    require_replace: bool = True
    allowed_instruments: frozenset[str] = frozenset()

    def validate(self) -> None:
        if self.maximum_plan_age <= timedelta(0) or self.maximum_evidence_age <= timedelta(0):
            raise ValueError("age limits must be positive")
        if self.maximum_quantity <= 0 or not self.maximum_quantity.is_finite():
            raise ValueError("maximum_quantity must be positive and finite")
        if self.maximum_notional <= 0 or not self.maximum_notional.is_finite():
            raise ValueError("maximum_notional must be positive and finite")
        normalized = frozenset(value.strip().upper() for value in self.allowed_instruments)
        if "" in normalized or normalized != self.allowed_instruments:
            raise ValueError("allowed_instruments must be normalized uppercase values")


@dataclass(frozen=True)
class RoundTripPlanV99:
    round_trip_id: str
    session_id: str
    account_id: str
    generation: int
    client_order_id: str
    instrument: str
    side: OrderSide
    quantity: Decimal
    limit_price: Decimal
    replacement_limit_price: Decimal | None
    created_at: datetime
    expires_at: datetime
    operator_approval_id: str
    approval_expires_at: datetime
    decision_digest: str
    external_order_routing_allowed: bool = False
    live_trading_allowed: bool = False
    digest: str = ""

    def validate(self) -> None:
        created_at = require_aware(self.created_at, field_name="created_at")
        expires_at = require_aware(self.expires_at, field_name="expires_at")
        approval_expires_at = require_aware(self.approval_expires_at, field_name="approval_expires_at")
        for name, value in (
            ("round_trip_id", self.round_trip_id),
            ("session_id", self.session_id),
            ("account_id", self.account_id),
            ("client_order_id", self.client_order_id),
            ("instrument", self.instrument),
            ("operator_approval_id", self.operator_approval_id),
        ):
            if not value.strip():
                raise ValueError(f"{name} is required")
        if self.instrument != self.instrument.upper():
            raise ValueError("instrument must be uppercase")
        if self.generation <= 0:
            raise ValueError("generation must be positive")
        if expires_at <= created_at or approval_expires_at < expires_at:
            raise ValueError("plan and approval windows are invalid")
        if self.quantity <= 0 or self.limit_price <= 0:
            raise ValueError("quantity and limit price must be positive")
        if self.replacement_limit_price is not None and self.replacement_limit_price <= 0:
            raise ValueError("replacement limit price must be positive")
        if len(self.decision_digest) != 64:
            raise ValueError("decision_digest must be SHA-256")
        if self.external_order_routing_allowed or self.live_trading_allowed:
            raise ValueError("live routing must remain disabled")
        if self.digest and self.digest != self.computed_digest():
            raise JournalCorruption("plan digest mismatch")

    def computed_digest(self) -> str:
        return sha256_digest(replace(self, digest=""))

    def sealed(self) -> "RoundTripPlanV99":
        value = replace(self, digest="")
        value.validate()
        return replace(value, digest=value.computed_digest())


@dataclass(frozen=True)
class AdmissionEvidenceV99:
    session_id: str
    generation: int
    captured_at: datetime
    session_running: bool
    paper_order_submission_allowed: bool
    platform_ready: bool
    broker_reliability_ready: bool
    qualification_ready: bool
    kill_switch_engaged: bool
    digest: str

    def validate(self) -> None:
        require_aware(self.captured_at, field_name="captured_at")
        if not self.session_id.strip() or self.generation <= 0:
            raise ValueError("session identity is invalid")
        if len(self.digest) != 64:
            raise ValueError("evidence digest must be SHA-256")
        if self.paper_order_submission_allowed and not self.session_running:
            raise ValueError("order admission requires running session")


@dataclass(frozen=True)
class RoundTripEventV99:
    sequence: int
    round_trip_id: str
    state: RoundTripState
    occurred_at: datetime
    generation: int
    attributes: Mapping[str, object]
    previous_digest: str
    digest: str

    def unsigned(self) -> Mapping[str, object]:
        return {
            "sequence": self.sequence,
            "round_trip_id": self.round_trip_id,
            "state": self.state,
            "occurred_at": self.occurred_at,
            "generation": self.generation,
            "attributes": dict(self.attributes),
            "previous_digest": self.previous_digest,
        }


class FileRoundTripJournalV99:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def append(
        self,
        *,
        round_trip_id: str,
        state: RoundTripState,
        occurred_at: datetime,
        generation: int,
        attributes: Mapping[str, object],
    ) -> RoundTripEventV99:
        occurred_at = require_aware(occurred_at, field_name="occurred_at")
        with self._lock:
            events = self.load()
            event = RoundTripEventV99(
                sequence=len(events) + 1,
                round_trip_id=round_trip_id,
                state=state,
                occurred_at=occurred_at,
                generation=generation,
                attributes=dict(attributes),
                previous_digest=events[-1].digest if events else _ZERO_DIGEST,
                digest="",
            )
            event = replace(event, digest=sha256_digest(event.unsigned()))
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(canonical_json(event) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return event

    def load(self) -> tuple[RoundTripEventV99, ...]:
        if not self.path.exists():
            return ()
        events: list[RoundTripEventV99] = []
        previous = _ZERO_DIGEST
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
            for index, line in enumerate(lines, start=1):
                raw = json.loads(line)
                event = RoundTripEventV99(
                    sequence=int(raw["sequence"]),
                    round_trip_id=str(raw["round_trip_id"]),
                    state=RoundTripState(str(raw["state"])),
                    occurred_at=datetime.fromisoformat(str(raw["occurred_at"]).replace("Z", "+00:00")),
                    generation=int(raw["generation"]),
                    attributes=dict(raw.get("attributes", {})),
                    previous_digest=str(raw["previous_digest"]),
                    digest=str(raw["digest"]),
                )
                if event.sequence != index or event.previous_digest != previous:
                    raise JournalCorruption("journal sequence or chain mismatch")
                if sha256_digest(event.unsigned()) != event.digest:
                    raise JournalCorruption("journal digest mismatch")
                if events and event.occurred_at < events[-1].occurred_at:
                    raise JournalCorruption("journal time regressed")
                events.append(event)
                previous = event.digest
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise JournalCorruption("invalid journal record") from exc
        return tuple(events)


@dataclass(frozen=True)
class RoundTripResultV99:
    round_trip_id: str
    state: RoundTripState
    outcome: RoundTripOutcome
    reasons: tuple[str, ...]
    broker_order_id: str
    filled_quantity: Decimal
    tail_digest: str
    paper_broker_mutation_verified: bool
    external_order_routing_allowed: bool = False
    live_trading_allowed: bool = False

    @property
    def success(self) -> bool:
        return self.state is RoundTripState.COMPLETED and self.outcome is RoundTripOutcome.CANCELLED_CLEAN


class PaperBrokerRoundTripServiceV99:
    """One bounded paper-only submit/replace/cancel/reconcile probe.

    Ambiguous mutations are never blindly retried. The service performs a read-only
    lookup and enters RECOVERING unless the broker state proves the mutation result.
    """

    def __init__(
        self,
        *,
        broker: PaperBrokerV99,
        plan: RoundTripPlanV99,
        evidence: AdmissionEvidenceV99,
        journal: FileRoundTripJournalV99,
        policy: RoundTripPolicyV99 = RoundTripPolicyV99(),
    ) -> None:
        policy.validate()
        plan.validate()
        evidence.validate()
        self.broker = broker
        self.plan = plan
        self.evidence = evidence
        self.journal = journal
        self.policy = policy

    def execute(self, *, now: datetime, expected_generation: int) -> RoundTripResultV99:
        now = require_aware(now, field_name="now")
        if expected_generation != self.plan.generation:
            raise StaleGeneration("round-trip generation is stale")
        previous = self.journal.load()
        if previous:
            return self._from_tail(previous[-1])
        reasons = self._preflight(now)
        if reasons:
            return self._finish(
                state=RoundTripState.BLOCKED,
                outcome=RoundTripOutcome.BLOCKED,
                now=now,
                reasons=reasons,
            )
        self.journal.append(
            round_trip_id=self.plan.round_trip_id,
            state=RoundTripState.PREFLIGHT,
            occurred_at=now,
            generation=self.plan.generation,
            attributes={"plan_digest": self.plan.digest},
        )
        try:
            order = self.broker.submit_limit_order(
                client_order_id=self.plan.client_order_id,
                instrument=self.plan.instrument,
                side=self.plan.side,
                quantity=self.plan.quantity,
                limit_price=self.plan.limit_price,
            )
        except BrokerMutationError as exc:
            if not exc.ambiguous:
                return self._finish(
                    state=RoundTripState.COMPLETED,
                    outcome=RoundTripOutcome.BROKER_REJECTED,
                    now=now,
                    reasons=(exc.code,),
                )
            order = self.broker.get_order_by_client_order_id(self.plan.client_order_id)
            if order is None:
                return self._finish(
                    state=RoundTripState.RECOVERING,
                    outcome=RoundTripOutcome.RECOVERY_REQUIRED,
                    now=now,
                    reasons=(f"AMBIGUOUS_SUBMIT:{exc.code}",),
                )
        blocked = self._exposure_result(order, now=now)
        if blocked:
            return blocked
        self._record(RoundTripState.SUBMITTED, order, now=now)
        if self.policy.require_replace:
            if self.plan.replacement_limit_price is None:
                return self._finish(
                    state=RoundTripState.BLOCKED,
                    outcome=RoundTripOutcome.BLOCKED,
                    now=now,
                    reasons=("REPLACEMENT_PRICE_REQUIRED",),
                )
            try:
                order = self.broker.replace_limit_order(
                    broker_order_id=order.broker_order_id,
                    limit_price=self.plan.replacement_limit_price,
                )
            except BrokerMutationError as exc:
                return self._recover_mutation(exc, now=now)
            blocked = self._exposure_result(order, now=now)
            if blocked:
                return blocked
            self._record(RoundTripState.REPLACED, order, now=now)
        try:
            order = self.broker.cancel_order(broker_order_id=order.broker_order_id)
        except BrokerMutationError as exc:
            return self._recover_mutation(exc, now=now)
        blocked = self._exposure_result(order, now=now)
        if blocked:
            return blocked
        if order.status is not BrokerOrderStatus.CANCELLED:
            return self._finish(
                state=RoundTripState.RECOVERING,
                outcome=RoundTripOutcome.RECOVERY_REQUIRED,
                now=now,
                reasons=("CANCEL_NOT_CONFIRMED",),
                order=order,
            )
        self._record(RoundTripState.CANCELLED, order, now=now)
        open_ids = {value.client_order_id for value in self.broker.list_open_orders()}
        if self.plan.client_order_id in open_ids:
            return self._finish(
                state=RoundTripState.RECOVERING,
                outcome=RoundTripOutcome.RECOVERY_REQUIRED,
                now=now,
                reasons=("ORDER_STILL_OPEN_AFTER_CANCEL",),
                order=order,
            )
        return self._finish(
            state=RoundTripState.COMPLETED,
            outcome=RoundTripOutcome.CANCELLED_CLEAN,
            now=now,
            reasons=(),
            order=order,
            verified=True,
        )

    def _preflight(self, now: datetime) -> tuple[str, ...]:
        reasons: list[str] = []
        if now < self.plan.created_at or now - self.plan.created_at > self.policy.maximum_plan_age:
            reasons.append("PLAN_STALE_OR_FROM_FUTURE")
        if now > self.plan.expires_at or now > self.plan.approval_expires_at:
            reasons.append("PLAN_OR_APPROVAL_EXPIRED")
        if self.evidence.session_id != self.plan.session_id:
            reasons.append("SESSION_ID_MISMATCH")
        if self.evidence.generation != self.plan.generation:
            reasons.append("SESSION_GENERATION_MISMATCH")
        if now < self.evidence.captured_at or now - self.evidence.captured_at > self.policy.maximum_evidence_age:
            reasons.append("SESSION_EVIDENCE_STALE")
        if not self.evidence.session_running or not self.evidence.paper_order_submission_allowed:
            reasons.append("PAPER_SESSION_NOT_RUNNING")
        if not self.evidence.platform_ready or not self.evidence.broker_reliability_ready:
            reasons.append("PLATFORM_OR_BROKER_NOT_READY")
        if not self.evidence.qualification_ready:
            reasons.append("QUALIFICATION_NOT_READY")
        if self.evidence.kill_switch_engaged:
            reasons.append("KILL_SWITCH_ENGAGED")
        if not bool(getattr(self.broker, "paper_order_writes_enabled", False)):
            reasons.append("PAPER_ORDER_WRITES_DISABLED")
        if self.plan.quantity > self.policy.maximum_quantity:
            reasons.append("QUANTITY_LIMIT_EXCEEDED")
        if self.plan.quantity * self.plan.limit_price > self.policy.maximum_notional:
            reasons.append("NOTIONAL_LIMIT_EXCEEDED")
        if self.policy.allowed_instruments and self.plan.instrument not in self.policy.allowed_instruments:
            reasons.append("INSTRUMENT_NOT_ALLOWLISTED")
        try:
            account = self.broker.get_account()
            account.validate()
            open_orders = self.broker.list_open_orders()
        except Exception as exc:
            reasons.append(f"BROKER_PREFLIGHT_FAILED:{type(exc).__name__}")
        else:
            if account.account_id != self.plan.account_id:
                reasons.append("BROKER_ACCOUNT_ID_MISMATCH")
            if account.status.upper() != self.policy.required_account_status.upper():
                reasons.append("BROKER_ACCOUNT_NOT_ACTIVE")
            if account.currency.upper() != self.policy.required_currency.upper():
                reasons.append("BROKER_ACCOUNT_CURRENCY_MISMATCH")
            if account.trading_blocked or account.buying_power <= 0:
                reasons.append("BROKER_ACCOUNT_BLOCKED")
            if any(value.client_order_id == self.plan.client_order_id for value in open_orders):
                reasons.append("UNOWNED_DUPLICATE_CLIENT_ORDER_ID")
        return tuple(sorted(set(reasons)))

    def _recover_mutation(self, exc: BrokerMutationError, *, now: datetime) -> RoundTripResultV99:
        if not exc.ambiguous:
            return self._finish(
                state=RoundTripState.BLOCKED,
                outcome=RoundTripOutcome.BLOCKED,
                now=now,
                reasons=(exc.code,),
            )
        order = self.broker.get_order_by_client_order_id(self.plan.client_order_id)
        if order is not None:
            blocked = self._exposure_result(order, now=now)
            if blocked:
                return blocked
        return self._finish(
            state=RoundTripState.RECOVERING,
            outcome=RoundTripOutcome.RECOVERY_REQUIRED,
            now=now,
            reasons=(f"AMBIGUOUS_MUTATION:{exc.code}",),
            order=order,
        )

    def _exposure_result(self, order: BrokerOrder, *, now: datetime) -> RoundTripResultV99 | None:
        order.validate()
        if order.client_order_id != self.plan.client_order_id:
            return self._finish(
                state=RoundTripState.BLOCKED,
                outcome=RoundTripOutcome.BLOCKED,
                now=now,
                reasons=("BROKER_ORDER_IDENTITY_MISMATCH",),
                order=order,
            )
        if order.filled_quantity > 0 or order.status in {
            BrokerOrderStatus.PARTIALLY_FILLED,
            BrokerOrderStatus.FILLED,
        }:
            return self._finish(
                state=RoundTripState.BLOCKED,
                outcome=RoundTripOutcome.RESIDUAL_PAPER_EXPOSURE,
                now=now,
                reasons=("RESIDUAL_PAPER_EXPOSURE",),
                order=order,
            )
        return None

    def _record(self, state: RoundTripState, order: BrokerOrder, *, now: datetime) -> None:
        self.journal.append(
            round_trip_id=self.plan.round_trip_id,
            state=state,
            occurred_at=now,
            generation=self.plan.generation,
            attributes={
                "broker_order_id": order.broker_order_id,
                "status": order.status.value,
                "filled_quantity": str(order.filled_quantity),
            },
        )

    def _finish(
        self,
        *,
        state: RoundTripState,
        outcome: RoundTripOutcome,
        now: datetime,
        reasons: tuple[str, ...],
        order: BrokerOrder | None = None,
        verified: bool = False,
    ) -> RoundTripResultV99:
        event = self.journal.append(
            round_trip_id=self.plan.round_trip_id,
            state=state,
            occurred_at=now,
            generation=self.plan.generation,
            attributes={
                "outcome": outcome.value,
                "reasons": reasons,
                "broker_order_id": "" if order is None else order.broker_order_id,
                "filled_quantity": "0" if order is None else str(order.filled_quantity),
                "verified": verified,
            },
        )
        return self._from_tail(event)

    def _from_tail(self, event: RoundTripEventV99) -> RoundTripResultV99:
        raw_outcome = event.attributes.get("outcome")
        outcome = RoundTripOutcome(str(raw_outcome)) if raw_outcome else RoundTripOutcome.RECOVERY_REQUIRED
        reasons_raw = event.attributes.get("reasons", ())
        reasons = tuple(str(value) for value in reasons_raw) if isinstance(reasons_raw, (list, tuple)) else ()
        return RoundTripResultV99(
            round_trip_id=event.round_trip_id,
            state=event.state,
            outcome=outcome,
            reasons=reasons,
            broker_order_id=str(event.attributes.get("broker_order_id", "")),
            filled_quantity=Decimal(str(event.attributes.get("filled_quantity", "0"))),
            tail_digest=event.digest,
            paper_broker_mutation_verified=bool(event.attributes.get("verified", False)),
            external_order_routing_allowed=False,
            live_trading_allowed=False,
        )
