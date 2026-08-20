from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.domain.trading import OrderIntent
from app.oms.postgres import PostgresOmsStore
from app.oms.store import DurableOmsStore, OrderRecord, OrderState

_BYBIT_ENTRY_PREFIX = "ASTRA-DEMO-E-"
_BYBIT_REDUCE_ONLY_PREFIXES = ("ASTRA-DEMO-C-", "ASTRA-DEMO-H-")
_BYBIT_OUTBOX_TOPIC = "bybit_order_submit"
_OPERATOR_MODES = frozenset({"RUNNING", "PAUSED", "READ_ONLY", "KILLED"})
_UNRESOLVED_ENTRY_STATES = (
    OrderState.OUTBOXED,
    OrderState.SUBMIT_STARTED,
    OrderState.UNCERTAIN,
    OrderState.RECONCILING,
    OrderState.MANUAL,
)


@dataclass(frozen=True)
class BybitEntrySubmissionClaim:
    record: OrderRecord
    mutation_allowed: bool
    claimed_now: bool


class PostgresBybitEntryOms(PostgresOmsStore):
    """Bybit broker adapter over the canonical PostgreSQL OMS state machine."""

    live_mainnet_order_routing_allowed = False
    automatic_resubmit_after_submit_started_allowed = False
    broker_name = "BYBIT_DEMO"

    def claim_entry_submission(
        self,
        intent: OrderIntent,
        *,
        client_order_id: str,
        occurred_at: datetime,
    ) -> BybitEntrySubmissionClaim:
        intent.validate()
        _validate_bybit_entry_id(client_order_id)
        if intent.intent_id != _intent_id(client_order_id):
            raise ValueError("Bybit OMS intent_id must derive from orderLinkId")
        if intent.symbol != intent.symbol.strip().upper() or not intent.symbol.endswith("USDT"):
            raise ValueError("Bybit OMS entry symbol must be normalized USDT")

        self.create(
            intent,
            client_order_id=client_order_id,
            occurred_at=occurred_at,
        )
        moment = self._now(occurred_at)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    current = self._load_for_update(cursor, intent.intent_id)
                    if current.client_order_id != client_order_id:
                        raise ValueError("Bybit OMS client order id changed")

                    if current.state in {OrderState.CREATED, OrderState.RISK_APPROVED}:
                        operator_mode, operator_generation = self._entry_operator_authority(cursor)
                        if operator_mode != "RUNNING":
                            if current.state is not OrderState.CREATED:
                                raise RuntimeError(
                                    "Bybit OMS operator blocked a non-atomic RISK_APPROVED entry"
                                )
                            DurableOmsStore._validate_transition(
                                current.state,
                                OrderState.REJECTED,
                            )
                            cursor.execute(
                                """UPDATE astra_oms_orders
                                SET state=%s, version=version+1, updated_at=%s
                                WHERE intent_id=%s""",
                                (OrderState.REJECTED.value, moment, intent.intent_id),
                            )
                            self._append_event(
                                cursor,
                                event_id=(
                                    f"bybit-operator-blocked:{intent.intent_id}:"
                                    f"{operator_generation}"
                                ),
                                intent_id=intent.intent_id,
                                event_type=OrderState.REJECTED.value,
                                payload={
                                    "broker": self.broker_name,
                                    "reason": "OPERATOR_NEW_ENTRY_BLOCKED",
                                    "operator_mode": operator_mode,
                                    "operator_generation": operator_generation,
                                    "network_mutation_attempted": False,
                                },
                                occurred_at=moment,
                            )
                            return BybitEntrySubmissionClaim(
                                record=self._load_for_update(cursor, intent.intent_id),
                                mutation_allowed=False,
                                claimed_now=False,
                            )
                    else:
                        operator_mode = None
                        operator_generation = None

                    if current.state is OrderState.CREATED:
                        DurableOmsStore._validate_transition(
                            current.state,
                            OrderState.RISK_APPROVED,
                        )
                        cursor.execute(
                            """UPDATE astra_oms_orders
                            SET state=%s, version=version+1, updated_at=%s
                            WHERE intent_id=%s""",
                            (OrderState.RISK_APPROVED.value, moment, intent.intent_id),
                        )
                        self._append_event(
                            cursor,
                            event_id=f"bybit-risk-approved:{intent.intent_id}",
                            intent_id=intent.intent_id,
                            event_type=OrderState.RISK_APPROVED.value,
                            payload={
                                "broker": self.broker_name,
                                "risk_source": "PREAPPROVED_BYBIT_ENTRY_PIPELINE",
                                "risk_recalculated_by_oms": False,
                                "operator_mode": operator_mode,
                                "operator_generation": operator_generation,
                            },
                            occurred_at=moment,
                        )
                        current = self._load_for_update(cursor, intent.intent_id)

                    if current.state is OrderState.RISK_APPROVED:
                        DurableOmsStore._validate_transition(current.state, OrderState.OUTBOXED)
                        payload = {
                            "intent_id": current.intent_id,
                            "client_order_id": current.client_order_id,
                            "symbol": current.symbol,
                            "side": current.side.value,
                            "quantity": str(current.quantity),
                            "reference_price": str(current.limit_price),
                            "price_role": "PRE_ENTRY_EXECUTABLE_QUOTE_REFERENCE",
                            "broker_order_type": "MARKET",
                            "broker": self.broker_name,
                            "dispatch_mode": "INLINE_AT_MOST_ONCE",
                        }
                        cursor.execute(
                            """INSERT INTO astra_oms_outbox
                            (intent_id, topic, payload, created_at, published_at)
                            VALUES (%s, %s, %s::jsonb, %s, %s)
                            ON CONFLICT (intent_id, topic) DO NOTHING""",
                            (
                                intent.intent_id,
                                _BYBIT_OUTBOX_TOPIC,
                                json.dumps(payload, sort_keys=True),
                                moment,
                                moment,
                            ),
                        )
                        if cursor.rowcount != 1:
                            raise RuntimeError("Bybit OMS inline submit claim already exists")
                        cursor.execute(
                            """UPDATE astra_oms_orders
                            SET state=%s, version=version+1, updated_at=%s
                            WHERE intent_id=%s""",
                            (OrderState.OUTBOXED.value, moment, intent.intent_id),
                        )
                        self._append_event(
                            cursor,
                            event_id=f"bybit-outboxed:{intent.intent_id}",
                            intent_id=intent.intent_id,
                            event_type=OrderState.OUTBOXED.value,
                            payload=payload,
                            occurred_at=moment,
                        )
                        current = self._load_for_update(cursor, intent.intent_id)
                        DurableOmsStore._validate_transition(
                            current.state,
                            OrderState.SUBMIT_STARTED,
                        )
                        cursor.execute(
                            """UPDATE astra_oms_orders
                            SET state=%s, version=version+1, updated_at=%s
                            WHERE intent_id=%s""",
                            (OrderState.SUBMIT_STARTED.value, moment, intent.intent_id),
                        )
                        self._append_event(
                            cursor,
                            event_id=f"bybit-submit-started:{intent.intent_id}",
                            intent_id=intent.intent_id,
                            event_type=OrderState.SUBMIT_STARTED.value,
                            payload={
                                "broker": self.broker_name,
                                "network_mutation_attempted": False,
                                "automatic_resubmit_allowed": False,
                            },
                            occurred_at=moment,
                        )
                        return BybitEntrySubmissionClaim(
                            record=self._load_for_update(cursor, intent.intent_id),
                            mutation_allowed=True,
                            claimed_now=True,
                        )

                    return BybitEntrySubmissionClaim(
                        record=current,
                        mutation_allowed=False,
                        claimed_now=False,
                    )

    def claim_reduce_only_submission(
        self,
        intent: OrderIntent,
        *,
        client_order_id: str,
        occurred_at: datetime,
    ) -> BybitEntrySubmissionClaim:
        """Durably claim a risk-reducing CLOSE without the new-entry operator gate."""

        intent.validate()
        _validate_bybit_reduce_only_id(client_order_id)
        if intent.intent_id != _reduce_only_intent_id(client_order_id):
            raise ValueError("Bybit reduce-only OMS intent_id must derive from orderLinkId")
        if intent.symbol != intent.symbol.strip().upper() or not intent.symbol.endswith("USDT"):
            raise ValueError("Bybit reduce-only OMS symbol must be normalized USDT")

        self.create(
            intent,
            client_order_id=client_order_id,
            occurred_at=occurred_at,
        )
        moment = self._now(occurred_at)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    current = self._load_for_update(cursor, intent.intent_id)
                    if current.client_order_id != client_order_id:
                        raise ValueError("Bybit reduce-only OMS client order id changed")

                    if current.state is OrderState.CREATED:
                        DurableOmsStore._validate_transition(
                            current.state,
                            OrderState.RISK_APPROVED,
                        )
                        cursor.execute(
                            """UPDATE astra_oms_orders
                            SET state=%s, version=version+1, updated_at=%s
                            WHERE intent_id=%s""",
                            (OrderState.RISK_APPROVED.value, moment, intent.intent_id),
                        )
                        self._append_event(
                            cursor,
                            event_id=f"bybit-risk-reduction-approved:{intent.intent_id}",
                            intent_id=intent.intent_id,
                            event_type=OrderState.RISK_APPROVED.value,
                            payload={
                                "broker": self.broker_name,
                                "risk_source": "PREAPPROVED_BYBIT_RISK_REDUCTION",
                                "risk_recalculated_by_oms": False,
                                "operator_entry_gate_applied": False,
                            },
                            occurred_at=moment,
                        )
                        current = self._load_for_update(cursor, intent.intent_id)

                    if current.state is OrderState.RISK_APPROVED:
                        DurableOmsStore._validate_transition(current.state, OrderState.OUTBOXED)
                        payload = {
                            "intent_id": current.intent_id,
                            "client_order_id": current.client_order_id,
                            "symbol": current.symbol,
                            "side": current.side.value,
                            "quantity": str(current.quantity),
                            "reference_price": str(current.limit_price),
                            "price_role": "RISK_REDUCTION_REFERENCE",
                            "order_role": "RISK_REDUCTION",
                            "reduce_only": True,
                            "broker_order_type": "MARKET",
                            "broker": self.broker_name,
                            "dispatch_mode": "INLINE_AT_MOST_ONCE",
                        }
                        cursor.execute(
                            """INSERT INTO astra_oms_outbox
                            (intent_id, topic, payload, created_at, published_at)
                            VALUES (%s, %s, %s::jsonb, %s, %s)
                            ON CONFLICT (intent_id, topic) DO NOTHING""",
                            (
                                intent.intent_id,
                                _BYBIT_OUTBOX_TOPIC,
                                json.dumps(payload, sort_keys=True),
                                moment,
                                moment,
                            ),
                        )
                        if cursor.rowcount != 1:
                            raise RuntimeError("Bybit reduce-only OMS inline submit claim already exists")
                        cursor.execute(
                            """UPDATE astra_oms_orders
                            SET state=%s, version=version+1, updated_at=%s
                            WHERE intent_id=%s""",
                            (OrderState.OUTBOXED.value, moment, intent.intent_id),
                        )
                        self._append_event(
                            cursor,
                            event_id=f"bybit-outboxed:{intent.intent_id}",
                            intent_id=intent.intent_id,
                            event_type=OrderState.OUTBOXED.value,
                            payload=payload,
                            occurred_at=moment,
                        )
                        current = self._load_for_update(cursor, intent.intent_id)
                        DurableOmsStore._validate_transition(
                            current.state,
                            OrderState.SUBMIT_STARTED,
                        )
                        cursor.execute(
                            """UPDATE astra_oms_orders
                            SET state=%s, version=version+1, updated_at=%s
                            WHERE intent_id=%s""",
                            (OrderState.SUBMIT_STARTED.value, moment, intent.intent_id),
                        )
                        self._append_event(
                            cursor,
                            event_id=f"bybit-submit-started:{intent.intent_id}",
                            intent_id=intent.intent_id,
                            event_type=OrderState.SUBMIT_STARTED.value,
                            payload={
                                "broker": self.broker_name,
                                "network_mutation_attempted": False,
                                "automatic_resubmit_allowed": False,
                                "order_role": "RISK_REDUCTION",
                            },
                            occurred_at=moment,
                        )
                        return BybitEntrySubmissionClaim(
                            record=self._load_for_update(cursor, intent.intent_id),
                            mutation_allowed=True,
                            claimed_now=True,
                        )

                    return BybitEntrySubmissionClaim(
                        record=current,
                        mutation_allowed=False,
                        claimed_now=False,
                    )

    @staticmethod
    def _entry_operator_authority(cursor) -> tuple[str, int]:
        cursor.execute(
            """SELECT mode, generation
            FROM astra_bybit_operator_state
            WHERE singleton=TRUE
            FOR SHARE"""
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("Bybit operator state is not initialized")
        mode = str(row["mode"])
        generation = int(row["generation"])
        if mode not in _OPERATOR_MODES or generation <= 0:
            raise ValueError("Bybit operator state is invalid")
        return mode, generation

    def mark_acknowledged(
        self,
        intent_id: str,
        *,
        broker_order_id: str,
        occurred_at: datetime,
        recovered_by_read: bool,
    ) -> OrderRecord:
        current = self.get(intent_id)
        if current is None:
            raise KeyError(intent_id)
        if current.state in {
            OrderState.ACKNOWLEDGED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
        }:
            if current.broker_order_id and current.broker_order_id != broker_order_id:
                raise ValueError("Bybit OMS broker order id changed")
            return current
        if current.state is not OrderState.SUBMIT_STARTED:
            raise ValueError(f"Bybit OMS cannot acknowledge state {current.state.value}")
        return self.transition(
            intent_id,
            OrderState.ACKNOWLEDGED,
            event_id=f"bybit-ack:{intent_id}:{broker_order_id}",
            occurred_at=occurred_at,
            broker_order_id=broker_order_id,
            payload={
                "broker": self.broker_name,
                "recovered_by_read": recovered_by_read,
            },
        )

    def mark_rejected(
        self,
        intent_id: str,
        *,
        occurred_at: datetime,
        reason: str,
        broker_order_id: str | None = None,
    ) -> OrderRecord:
        current = self.get(intent_id)
        if current is None:
            raise KeyError(intent_id)
        if current.state is OrderState.REJECTED:
            if broker_order_id and current.broker_order_id not in {"", broker_order_id}:
                raise ValueError("Bybit OMS broker order id changed")
            return current
        if current.state is not OrderState.SUBMIT_STARTED:
            raise ValueError(f"Bybit OMS cannot reject state {current.state.value}")
        return self.transition(
            intent_id,
            OrderState.REJECTED,
            event_id=f"bybit-rejected:{intent_id}",
            occurred_at=occurred_at,
            broker_order_id=broker_order_id,
            payload={"broker": self.broker_name, "reason": reason},
        )

    def mark_uncertain(
        self,
        intent_id: str,
        *,
        occurred_at: datetime,
        reason: str,
    ) -> OrderRecord:
        current = self.get(intent_id)
        if current is None:
            raise KeyError(intent_id)
        if current.state in {
            OrderState.UNCERTAIN,
            OrderState.RECONCILING,
            OrderState.MANUAL,
        }:
            return current
        if current.state is not OrderState.SUBMIT_STARTED:
            raise ValueError(f"Bybit OMS cannot mark uncertain state {current.state.value}")
        return self.transition(
            intent_id,
            OrderState.UNCERTAIN,
            event_id=f"bybit-uncertain:{intent_id}",
            occurred_at=occurred_at,
            payload={"broker": self.broker_name, "reason": reason},
        )

    def unresolved_entry_submissions(self) -> tuple[OrderRecord, ...]:
        states = tuple(state.value for state in _UNRESOLVED_ENTRY_STATES)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT * FROM astra_oms_orders
                    WHERE client_order_id LIKE %s AND state = ANY(%s)
                    ORDER BY updated_at, intent_id""",
                    (f"{_BYBIT_ENTRY_PREFIX}%", list(states)),
                )
                return tuple(self._row(row) for row in cursor.fetchall())

    def count_unresolved_entry_submissions(self) -> int:
        return len(self.unresolved_entry_submissions())

    def count_uncertain_entries(self) -> int:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT count(*) AS uncertain_count
                    FROM astra_oms_orders
                    WHERE client_order_id LIKE %s AND state=%s""",
                    (f"{_BYBIT_ENTRY_PREFIX}%", OrderState.UNCERTAIN.value),
                )
                row = cursor.fetchone()
        if row is None:
            raise RuntimeError("Bybit OMS uncertain-count query returned no row")
        return int(row["uncertain_count"])

    def mark_lifecycle_reconciliation_required(
        self,
        intent_id: str,
        *,
        broker_order_id: str,
        broker_status: str,
        cumulative_executed_quantity: Decimal,
        occurred_at: datetime,
    ) -> OrderRecord:
        if not broker_order_id.strip() or not broker_status.strip():
            raise ValueError("broker order id and status are required for reconciliation")
        if not cumulative_executed_quantity.is_finite() or cumulative_executed_quantity < 0:
            raise ValueError("cumulative executed quantity must be finite and non-negative")
        current = self.get(intent_id)
        if current is None:
            raise KeyError(intent_id)
        if current.state is OrderState.SUBMIT_STARTED:
            current = self.mark_uncertain(
                intent_id,
                occurred_at=occurred_at,
                reason="BROKER_ORDER_FOUND_REQUIRES_LIFECYCLE_RECONCILIATION",
            )
        if current.state is OrderState.UNCERTAIN:
            return self.transition(
                intent_id,
                OrderState.RECONCILING,
                event_id=f"bybit-reconciling:{intent_id}:{broker_order_id}",
                occurred_at=occurred_at,
                broker_order_id=broker_order_id,
                payload={
                    "broker": self.broker_name,
                    "broker_status": broker_status,
                    "cumulative_executed_quantity": str(cumulative_executed_quantity),
                    "lifecycle_reconciliation_required": True,
                },
            )
        if current.state is OrderState.RECONCILING:
            if current.broker_order_id not in {"", broker_order_id}:
                raise ValueError("Bybit OMS broker order id changed during reconciliation")
            return current
        raise ValueError(
            f"Bybit OMS cannot require lifecycle reconciliation from {current.state.value}"
        )

    def resolve_rejected_without_execution(
        self,
        intent_id: str,
        *,
        broker_order_id: str,
        cumulative_executed_quantity: Decimal,
        occurred_at: datetime,
    ) -> OrderRecord:
        if cumulative_executed_quantity != 0:
            raise ValueError("safe rejected resolution requires zero cumulative execution")
        current = self.get(intent_id)
        if current is None:
            raise KeyError(intent_id)
        if current.state is OrderState.REJECTED:
            return current
        if current.state is OrderState.SUBMIT_STARTED:
            return self.mark_rejected(
                intent_id,
                occurred_at=occurred_at,
                reason="BROKER_TRUTH_REJECTED_WITH_ZERO_EXECUTION",
                broker_order_id=broker_order_id,
            )
        if current.state is OrderState.UNCERTAIN:
            current = self.transition(
                intent_id,
                OrderState.RECONCILING,
                event_id=f"bybit-reconciling-rejected:{intent_id}:{broker_order_id}",
                occurred_at=occurred_at,
                broker_order_id=broker_order_id,
                payload={
                    "broker": self.broker_name,
                    "broker_status": "Rejected",
                    "cumulative_executed_quantity": "0",
                },
            )
        if current.state is OrderState.RECONCILING:
            current = self.transition(
                intent_id,
                OrderState.RECONCILED,
                event_id=f"bybit-reconciled-rejected:{intent_id}:{broker_order_id}",
                occurred_at=occurred_at,
                broker_order_id=broker_order_id,
                payload={
                    "broker": self.broker_name,
                    "broker_status": "Rejected",
                    "zero_execution_proven": True,
                },
            )
        if current.state is OrderState.RECONCILED:
            return self.transition(
                intent_id,
                OrderState.REJECTED,
                event_id=f"bybit-rejected-after-reconcile:{intent_id}:{broker_order_id}",
                occurred_at=occurred_at,
                broker_order_id=broker_order_id,
                payload={
                    "broker": self.broker_name,
                    "reason": "BROKER_TRUTH_REJECTED_WITH_ZERO_EXECUTION",
                },
            )
        raise ValueError(f"Bybit OMS cannot safely resolve state {current.state.value} as rejected")


def bybit_entry_intent_id(order_link_id: str) -> str:
    _validate_bybit_entry_id(order_link_id)
    return _intent_id(order_link_id)


def bybit_reduce_only_intent_id(order_link_id: str) -> str:
    _validate_bybit_reduce_only_id(order_link_id)
    return _reduce_only_intent_id(order_link_id)


def _intent_id(order_link_id: str) -> str:
    return f"bybit-entry:{order_link_id}"


def _reduce_only_intent_id(order_link_id: str) -> str:
    return f"bybit-reduce-only:{order_link_id}"


def _validate_bybit_entry_id(order_link_id: str) -> None:
    if not order_link_id.startswith(_BYBIT_ENTRY_PREFIX):
        raise ValueError("Bybit entry OMS requires ASTRA-DEMO-E orderLinkId")
    if len(order_link_id) > 36:
        raise ValueError("Bybit entry OMS orderLinkId exceeds Bybit limit")


def _validate_bybit_reduce_only_id(order_link_id: str) -> None:
    if not order_link_id.startswith(_BYBIT_REDUCE_ONLY_PREFIXES):
        raise ValueError("Bybit reduce-only OMS requires deterministic CLOSE orderLinkId")
    if len(order_link_id) > 36:
        raise ValueError("Bybit reduce-only OMS orderLinkId exceeds Bybit limit")
