from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from app.domain.trading import Side
from app.oms.order_mutations import (
    ActiveMutationExists,
    MutationStore,
    OrderMutationLifecycle,
    OrderMutationRecord,
)
from app.oms.protocols import OmsStore
from app.oms.store import OrderState


class ProtectiveRepriceStatus(StrEnum):
    NOT_NEEDED = "NOT_NEEDED"
    WAITING_FOR_ACTIVE_ORDER = "WAITING_FOR_ACTIVE_ORDER"
    ACTIVE_MUTATION_EXISTS = "ACTIVE_MUTATION_EXISTS"
    REQUESTED = "REQUESTED"
    TERMINAL_ORDER = "TERMINAL_ORDER"


@dataclass(frozen=True)
class ProtectiveSellRepricePolicy:
    minimum_adverse_move_fraction: Decimal = Decimal("0.0025")
    marketability_cushion_fraction: Decimal = Decimal("0")

    def validate(self) -> None:
        for field_name, value in (
            ("minimum_adverse_move_fraction", self.minimum_adverse_move_fraction),
            ("marketability_cushion_fraction", self.marketability_cushion_fraction),
        ):
            if not value.is_finite() or value < 0 or value >= 1:
                raise ValueError(f"{field_name} must be finite and within [0, 1)")


@dataclass(frozen=True)
class ProtectiveRepriceDecision:
    status: ProtectiveRepriceStatus
    intent_id: str
    current_limit_price: Decimal
    target_limit_price: Decimal
    adverse_move_fraction: Decimal
    mutation: OrderMutationRecord | None = None


class PaperProtectionRepricePlanner:
    """Request a durable adverse-only limit replacement for one protective SELL.

    This component never calls a broker. It writes through ``OrderMutationLifecycle``
    so the existing at-most-once mutation executor can later perform the paper PATCH
    and recover ambiguous outcomes with GET-only reads.
    """

    _ACTIVE = frozenset({OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED})
    _WAITING = frozenset(
        {
            OrderState.CREATED,
            OrderState.RISK_APPROVED,
            OrderState.OUTBOXED,
            OrderState.SUBMIT_STARTED,
            OrderState.CANCEL_REQUESTED,
            OrderState.UNCERTAIN,
            OrderState.RECONCILING,
            OrderState.RECONCILED,
            OrderState.MANUAL,
        }
    )
    _TERMINAL = frozenset(
        {OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED}
    )

    def __init__(
        self,
        *,
        oms: OmsStore,
        mutation_lifecycle: OrderMutationLifecycle,
        mutations: MutationStore,
        policy: ProtectiveSellRepricePolicy | None = None,
    ) -> None:
        resolved = ProtectiveSellRepricePolicy() if policy is None else policy
        resolved.validate()
        if mutation_lifecycle.oms is not oms:
            raise ValueError("mutation lifecycle and reprice planner must share OMS")
        if mutation_lifecycle.mutations is not mutations:
            raise ValueError("mutation lifecycle and reprice planner must share store")
        self.oms = oms
        self.mutation_lifecycle = mutation_lifecycle
        self.mutations = mutations
        self.policy = resolved

    def consider(
        self,
        *,
        intent_id: str,
        reference_price: Decimal,
        observed_at: datetime,
    ) -> ProtectiveRepriceDecision:
        if not reference_price.is_finite() or reference_price <= 0:
            raise ValueError("reference_price must be positive and finite")
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        order = self.oms.get(intent_id)
        if order is None:
            raise KeyError(intent_id)
        if order.side is not Side.SELL:
            raise ValueError("PROTECTIVE_REPRICE_REQUIRES_SELL")

        current_limit = self.mutations.current_limit_price(
            intent_id,
            fallback=order.limit_price,
        )
        target = reference_price * (
            Decimal("1") - self.policy.marketability_cushion_fraction
        )
        adverse = max(
            Decimal("0"),
            (current_limit - target) / current_limit,
        )
        if order.state in self._TERMINAL:
            return ProtectiveRepriceDecision(
                status=ProtectiveRepriceStatus.TERMINAL_ORDER,
                intent_id=intent_id,
                current_limit_price=current_limit,
                target_limit_price=target,
                adverse_move_fraction=adverse,
            )
        if order.state in self._WAITING:
            return ProtectiveRepriceDecision(
                status=ProtectiveRepriceStatus.WAITING_FOR_ACTIVE_ORDER,
                intent_id=intent_id,
                current_limit_price=current_limit,
                target_limit_price=target,
                adverse_move_fraction=adverse,
            )
        if order.state not in self._ACTIVE:
            raise RuntimeError(f"unsupported protection reprice state:{order.state.value}")
        if target >= current_limit or adverse < self.policy.minimum_adverse_move_fraction:
            return ProtectiveRepriceDecision(
                status=ProtectiveRepriceStatus.NOT_NEEDED,
                intent_id=intent_id,
                current_limit_price=current_limit,
                target_limit_price=target,
                adverse_move_fraction=adverse,
            )

        mutation_id = self._mutation_id(intent_id=intent_id, target_limit_price=target)
        try:
            mutation = self.mutation_lifecycle.request_replace(
                intent_id,
                mutation_id=mutation_id,
                target_limit_price=target,
                occurred_at=observed_at,
            )
        except ActiveMutationExists:
            return ProtectiveRepriceDecision(
                status=ProtectiveRepriceStatus.ACTIVE_MUTATION_EXISTS,
                intent_id=intent_id,
                current_limit_price=current_limit,
                target_limit_price=target,
                adverse_move_fraction=adverse,
            )
        return ProtectiveRepriceDecision(
            status=ProtectiveRepriceStatus.REQUESTED,
            intent_id=intent_id,
            current_limit_price=current_limit,
            target_limit_price=target,
            adverse_move_fraction=adverse,
            mutation=mutation,
        )

    @staticmethod
    def _mutation_id(*, intent_id: str, target_limit_price: Decimal) -> str:
        canonical = format(target_limit_price.normalize(), "f")
        digest = hashlib.sha256(
            f"protective-reprice|{intent_id}|{canonical}".encode()
        ).hexdigest()
        return f"protective-reprice:{digest}"
