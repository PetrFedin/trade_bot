from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from app.domain.trading import OrderIntent
from app.oms.postgres import PostgresOmsStore
from app.oms.store import DurableOmsStore, OrderRecord, OrderState

_BYBIT_ENTRY_PREFIX = "ASTRA-DEMO-E-"
_BYBIT_OUTBOX_TOPIC = "bybit_order_submit"
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
    """Bybit ENTRY broker adapter over the canonical PostgreSQL OMS state machine.

    The adapter uses the existing ``astra_oms_*`` tables and ``OrderState`` transitions. A Bybit
    submit is claimed inline: OUTBOXED and SUBMIT_STARTED are durable before the network POST, and
    the broker-specific outbox row is marked published in the same transaction so a generic paper
    worker cannot consume it. A restart may recover by broker reads, but must never POST again.
    """

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
    ) -> OrderRecord:
        current = self.get(intent_id)
        if current is None:
            raise KeyError(intent_id)
        if current.state is OrderState.REJECTED:
            return current
        if current.state is not OrderState.SUBMIT_STARTED:
            raise ValueError(f"Bybit OMS cannot reject state {current.state.value}")
        return self.transition(
            intent_id,
            OrderState.REJECTED,
            event_id=f"bybit-rejected:{intent_id}",
            occurred_at=occurred_at,
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

    def count_unresolved_entry_submissions(self) -> int:
        states = tuple(state.value for state in _UNRESOLVED_ENTRY_STATES)
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT count(*) AS unresolved_count
                    FROM astra_oms_orders
                    WHERE client_order_id LIKE %s
                      AND state = ANY(%s)""",
                    (f"{_BYBIT_ENTRY_PREFIX}%", list(states)),
                )
                row = cursor.fetchone()
        if row is None:
            raise RuntimeError("Bybit OMS unresolved-count query returned no row")
        return int(row["unresolved_count"])

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


def bybit_entry_intent_id(order_link_id: str) -> str:
    _validate_bybit_entry_id(order_link_id)
    return _intent_id(order_link_id)


def _intent_id(order_link_id: str) -> str:
    return f"bybit-entry:{order_link_id}"


def _validate_bybit_entry_id(order_link_id: str) -> None:
    if not order_link_id.startswith(_BYBIT_ENTRY_PREFIX):
        raise ValueError("Bybit entry OMS requires ASTRA-DEMO-E orderLinkId")
    if len(order_link_id) > 36:
        raise ValueError("Bybit entry OMS orderLinkId exceeds Bybit limit")
