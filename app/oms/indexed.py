from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.domain.trading import Side
from app.oms.postgres import PostgresOmsStore
from app.oms.protocols import OmsStore
from app.oms.risk_reservations import (
    PendingBuyExposure,
    RiskReservationBudget,
    RiskReservationRejected,
    evaluate_buy_reservation,
)
from app.oms.store import DurableOmsStore, OrderRecord, OrderState

_RISK_RESERVING_STATES = (
    OrderState.RISK_APPROVED,
    OrderState.OUTBOXED,
    OrderState.SUBMIT_STARTED,
    OrderState.ACKNOWLEDGED,
    OrderState.PARTIALLY_FILLED,
    OrderState.CANCEL_REQUESTED,
    OrderState.UNCERTAIN,
    OrderState.RECONCILING,
    OrderState.RECONCILED,
    OrderState.MANUAL,
)
_POSTGRES_RISK_RESERVATION_LOCK_KEY = 0x41535452


class IndexedOmsStore(OmsStore, Protocol):
    """OMS port with durable broker identity lookup and atomic risk reservation."""

    def get_by_client_order_id(self, client_order_id: str) -> OrderRecord | None: ...

    def get_by_broker_order_id(self, broker_order_id: str) -> OrderRecord | None: ...

    def approve_risk_with_reservation(
        self,
        intent_id: str,
        *,
        event_id: str,
        occurred_at: datetime,
        budget: RiskReservationBudget,
    ) -> OrderRecord: ...


class IndexedDurableOmsStore(DurableOmsStore):
    def get_by_client_order_id(self, client_order_id: str) -> OrderRecord | None:
        normalized = client_order_id.strip()
        if not normalized:
            raise ValueError("client_order_id is required")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM oms_orders WHERE client_order_id=?",
                (normalized,),
            ).fetchone()
            return None if row is None else self._row(row)
        finally:
            connection.close()

    def get_by_broker_order_id(self, broker_order_id: str) -> OrderRecord | None:
        normalized = broker_order_id.strip()
        if not normalized:
            raise ValueError("broker_order_id is required")
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM oms_orders WHERE broker_order_id=? ORDER BY intent_id",
                (normalized,),
            ).fetchall()
            if len(rows) > 1:
                raise ValueError("OMS_BROKER_ORDER_ID_CONFLICT")
            return None if not rows else self._row(rows[0])
        finally:
            connection.close()

    def approve_risk_with_reservation(
        self,
        intent_id: str,
        *,
        event_id: str,
        occurred_at: datetime,
        budget: RiskReservationBudget,
    ) -> OrderRecord:
        """Atomically reserve pending BUY capacity before RISK_APPROVED."""

        budget.validate()
        moment = self._now(occurred_at)
        states = tuple(state.value for state in _RISK_RESERVING_STATES)
        with self._transaction() as connection:
            current = self._load_for_update(connection, intent_id)
            if connection.execute(
                "SELECT 1 FROM oms_events WHERE event_id=?", (event_id,)
            ).fetchone():
                return current
            self._validate_transition(current.state, OrderState.RISK_APPROVED)
            if current.side is Side.BUY:
                rows = connection.execute(
                    """SELECT * FROM oms_orders
                    WHERE side='BUY'
                      AND state IN (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    states,
                ).fetchall()
                active_buys = tuple(
                    PendingBuyExposure(
                        symbol=record.symbol,
                        remaining_notional=(record.quantity - record.filled_quantity)
                        * record.limit_price,
                    )
                    for record in (self._row(row) for row in rows)
                )
                evaluation = evaluate_buy_reservation(
                    symbol=current.symbol,
                    candidate_notional=current.quantity * current.limit_price,
                    budget=budget,
                    active_buys=active_buys,
                )
                if not evaluation.approved:
                    raise RiskReservationRejected(evaluation.reasons)
                payload = evaluation.event_payload()
            else:
                payload = {"reservation": {"approved": True, "kind": "SELL_NO_CAPACITY_CREDIT"}}
            connection.execute(
                """UPDATE oms_orders
                SET state=?, version=version+1, updated_at=? WHERE intent_id=?""",
                (OrderState.RISK_APPROVED.value, moment.isoformat(), intent_id),
            )
            self._append_event(
                connection,
                event_id=event_id,
                intent_id=intent_id,
                event_type=OrderState.RISK_APPROVED.value,
                payload=payload,
                occurred_at=moment,
            )
            return self._load_for_update(connection, intent_id)


class IndexedPostgresOmsStore(PostgresOmsStore):
    def get_by_client_order_id(self, client_order_id: str) -> OrderRecord | None:
        normalized = client_order_id.strip()
        if not normalized:
            raise ValueError("client_order_id is required")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM astra_oms_orders WHERE client_order_id=%s",
                    (normalized,),
                )
                row = cursor.fetchone()
                return None if row is None else self._row(row)

    def get_by_broker_order_id(self, broker_order_id: str) -> OrderRecord | None:
        normalized = broker_order_id.strip()
        if not normalized:
            raise ValueError("broker_order_id is required")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT * FROM astra_oms_orders
                    WHERE broker_order_id=%s ORDER BY intent_id LIMIT 2""",
                    (normalized,),
                )
                rows = cursor.fetchall()
                if len(rows) > 1:
                    raise ValueError("OMS_BROKER_ORDER_ID_CONFLICT")
                return None if not rows else self._row(rows[0])

    def approve_risk_with_reservation(
        self,
        intent_id: str,
        *,
        event_id: str,
        occurred_at: datetime,
        budget: RiskReservationBudget,
    ) -> OrderRecord:
        """Serialize account-wide approval and reserve existing pending BUY risk."""

        budget.validate()
        moment = self._now(occurred_at)
        states = [state.value for state in _RISK_RESERVING_STATES]
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    # The current canonical product has one trading account per database.
                    # Serialize reservation decisions across different order rows so two
                    # workers cannot both approve against the same available capacity.
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(%s)",
                        (_POSTGRES_RISK_RESERVATION_LOCK_KEY,),
                    )
                    current = self._load_for_update(cursor, intent_id)
                    if self._event_exists(cursor, event_id):
                        return current
                    DurableOmsStore._validate_transition(
                        current.state, OrderState.RISK_APPROVED
                    )
                    if current.side is Side.BUY:
                        cursor.execute(
                            """SELECT * FROM astra_oms_orders
                            WHERE side='BUY' AND state = ANY(%s)""",
                            (states,),
                        )
                        active_buys = tuple(
                            PendingBuyExposure(
                                symbol=record.symbol,
                                remaining_notional=(
                                    record.quantity - record.filled_quantity
                                )
                                * record.limit_price,
                            )
                            for record in (self._row(row) for row in cursor.fetchall())
                        )
                        evaluation = evaluate_buy_reservation(
                            symbol=current.symbol,
                            candidate_notional=current.quantity * current.limit_price,
                            budget=budget,
                            active_buys=active_buys,
                        )
                        if not evaluation.approved:
                            raise RiskReservationRejected(evaluation.reasons)
                        payload = evaluation.event_payload()
                    else:
                        payload = {
                            "reservation": {
                                "approved": True,
                                "kind": "SELL_NO_CAPACITY_CREDIT",
                            }
                        }
                    cursor.execute(
                        """UPDATE astra_oms_orders
                        SET state=%s, version=version+1, updated_at=%s
                        WHERE intent_id=%s""",
                        (OrderState.RISK_APPROVED.value, moment, intent_id),
                    )
                    self._append_event(
                        cursor,
                        event_id=event_id,
                        intent_id=intent_id,
                        event_type=OrderState.RISK_APPROVED.value,
                        payload=payload,
                        occurred_at=moment,
                    )
                    return self._load_for_update(cursor, intent_id)
