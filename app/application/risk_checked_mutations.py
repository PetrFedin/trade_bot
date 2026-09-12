from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal

from app.domain.trading import OrderIntent, Side
from app.oms.order_mutations import MutationStore, OrderMutationLifecycle, OrderMutationRecord
from app.oms.store import OrderRecord
from app.risk.evidence import RiskAdmissionService
from app.risk.pretrade import OperationalRiskContext


def replace_requires_risk_readmission(
    order: OrderRecord,
    *,
    baseline_limit_price: Decimal,
    target_limit_price: Decimal,
) -> bool:
    """Return True only when a replace can increase long-side committed notional."""

    return order.side is Side.BUY and target_limit_price > baseline_limit_price


def replace_risk_intent(
    order: OrderRecord,
    *,
    mutation_id: str,
    baseline_limit_price: Decimal,
    target_limit_price: Decimal,
) -> OrderIntent:
    """Build a deterministic synthetic intent for replace re-admission evidence."""

    if not mutation_id.strip():
        raise ValueError("mutation_id is required")
    if not baseline_limit_price.is_finite() or baseline_limit_price <= 0:
        raise ValueError("baseline_limit_price must be positive and finite")
    if not target_limit_price.is_finite() or target_limit_price <= 0:
        raise ValueError("target_limit_price must be positive and finite")
    remaining_quantity = order.quantity - order.filled_quantity
    if not remaining_quantity.is_finite() or remaining_quantity <= 0:
        raise ValueError("REPLACE_REMAINING_QUANTITY_REQUIRED")

    material = json.dumps(
        {
            "source_intent_id": order.intent_id,
            "mutation_id": mutation_id,
            "symbol": order.symbol,
            "side": order.side.value,
            "remaining_quantity": str(remaining_quantity),
            "baseline_limit_price": str(baseline_limit_price),
            "target_limit_price": str(target_limit_price),
            "source_updated_at": order.updated_at.astimezone(UTC).isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return OrderIntent(
        intent_id=f"replace-risk:{digest}",
        symbol=order.symbol,
        side=order.side,
        quantity=remaining_quantity,
        limit_price=target_limit_price,
        created_at=order.updated_at,
        strategy_id=f"replace-risk:{order.intent_id}",
    )


class RiskCheckedOrderMutationLifecycle(OrderMutationLifecycle):
    """Canonical mutation lifecycle with mandatory BUY replace risk re-admission.

    Cancel operations and risk-reducing replaces stay available without a new
    admission. A BUY price increase has no permissive defaults: callers must
    supply authoritative exposure inputs and a complete operational risk context.
    The decision is persisted by ``RiskAdmissionService`` before the mutation or
    outbox row can exist.
    """

    def __init__(
        self,
        *,
        oms,
        mutations: MutationStore,
        risk_admission: RiskAdmissionService,
    ) -> None:
        super().__init__(oms=oms, mutations=mutations)
        self.risk_admission = risk_admission

    def request_replace(
        self,
        intent_id: str,
        *,
        mutation_id: str,
        target_limit_price: Decimal,
        occurred_at: datetime,
        current_symbol_notional: Decimal | None = None,
        current_gross_notional: Decimal | None = None,
        risk_context: OperationalRiskContext | None = None,
        kill_switch_engaged: bool = False,
    ) -> OrderMutationRecord:
        if not target_limit_price.is_finite() or target_limit_price <= 0:
            raise ValueError("target_limit_price must be positive and finite")

        existing = self.mutations.get(mutation_id)
        if existing is not None:
            return super().request_replace(
                intent_id,
                mutation_id=mutation_id,
                target_limit_price=target_limit_price,
                occurred_at=occurred_at,
            )

        order = self._active_order(intent_id)
        baseline = self.mutations.current_limit_price(intent_id, fallback=order.limit_price)
        if replace_requires_risk_readmission(
            order,
            baseline_limit_price=baseline,
            target_limit_price=target_limit_price,
        ):
            if current_symbol_notional is None or current_gross_notional is None:
                raise ValueError("REPLACE_RISK_EXPOSURE_CONTEXT_REQUIRED")
            if risk_context is None:
                raise ValueError("REPLACE_RISK_CONTEXT_REQUIRED")
            risk_context.validate()
            if risk_context.decision_time != occurred_at:
                raise ValueError("REPLACE_RISK_DECISION_TIME_MISMATCH")

            risk_intent = replace_risk_intent(
                order,
                mutation_id=mutation_id,
                baseline_limit_price=baseline,
                target_limit_price=target_limit_price,
            )
            recorded = self.risk_admission.evaluate_and_record(
                risk_intent,
                current_symbol_notional=current_symbol_notional,
                current_gross_notional=current_gross_notional,
                kill_switch_engaged=kill_switch_engaged,
                context=risk_context.to_risk_context(),
                evaluated_at=occurred_at,
            )
            if not recorded.decision.approved:
                reasons = ",".join(recorded.decision.reasons) or "UNKNOWN"
                raise ValueError(f"REPLACE_RISK_NOT_APPROVED:{reasons}")

        return super().request_replace(
            intent_id,
            mutation_id=mutation_id,
            target_limit_price=target_limit_price,
            occurred_at=occurred_at,
        )
